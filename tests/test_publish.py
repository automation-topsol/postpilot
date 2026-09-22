"""The failure-injection suite.

Every scenario in the brief's §10 table lives here, plus the state-machine
units underneath them. These are the tests that make the delivery guarantee
real, so each one is named for the situation rather than the function:

    PostPilot never knowingly publishes the same post to the same platform
    twice. Once success is recorded for a platform, it is never automatically
    published again. If a remote API result is ambiguous, PostPilot stops and
    asks a human instead of retrying.

No test here touches a real API, Google, or the network.
"""

from __future__ import annotations

import datetime as dt

import pytest

from postpilot.media.store import InMemoryStore
from postpilot.models import Brand, Platform, PlatformState, RowStatus, StateRow
from postpilot.publish import publish, record_result, select_due
from postpilot.publishers.base import (
    BrandCreds,
    PublishResult,
    RemotePost,
    backoff_delay,
)
from postpilot.sheets.schema import BRANDS_HEADERS, LOG_HEADERS, STATE_HEADERS, brand_headers
from postpilot.sheets.state import state_to_row
from tests.conftest import TZ, make_row
from tests.fakes import FakePublisher, FakeSheetClient, state_column
from tests.test_drive_prepare import FakeDrive, drive_file

HEADERS = brand_headers()
NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)
DUE_DATE, DUE_TIME = "2026-10-07", "18:00"  # 13:00 UTC — an hour before NOW

BRAND_ROW = make_row(
    BRANDS_HEADERS,
    **{
        "Brand Name": "Grand Invitation",
        "Slug": "grandinvitation",
        "Enabled Platforms": "FB, IG, LI",
        "Facebook Page ID": "1355072654348977",
        "Instagram User ID": "17841432916654917",
        "LinkedIn Org URN": "urn:li:organization:99",
        "Drive Folder ID": "folder",
        "Active": "TRUE",
    },
)

BRAND = Brand(
    name="Grand Invitation",
    slug="grandinvitation",
    enabled_platforms=[Platform.FB, Platform.IG, Platform.LI],
    facebook_page_id="1355072654348977",
    instagram_user_id="17841432916654917",
    linkedin_org_urn="urn:li:organization:99",
    drive_folder_id="folder",
)

CREDS = {"grandinvitation": BrandCreds(brand=BRAND, meta_page_token="tok", linkedin_access_token="li")}


def post_row(**values: str) -> list[str]:
    base = {
        "ID": "gi-0001",
        "Date": DUE_DATE,
        "Time": DUE_TIME,
        "Platforms": "FB, IG",
        "Type": "image",
        "Media": "a.png",
        "Caption": "Every love story deserves a beginning worth remembering.",
    }
    base.update(values)
    return make_row(HEADERS, **base)


def build(rows, states=None) -> FakeSheetClient:
    return FakeSheetClient(
        {
            "_Brands": (BRANDS_HEADERS, [BRAND_ROW]),
            "_State": (STATE_HEADERS, [state_to_row(s) for s in (states or [])]),
            "_Log": (LOG_HEADERS, []),
            "grandinvitation": (HEADERS, rows),
        }
    )


def drive() -> FakeDrive:
    return FakeDrive([drive_file("a.png", "id-a", md5="md5a"), drive_file("b.png", "id-b", md5="md5b")])


def run(client, publishers, **kwargs):
    defaults = dict(tz_name=TZ, now=NOW, lease_minutes=20, max_attempts=3, backoff_base=300)
    defaults.update(kwargs)
    return publish(client, drive(), InMemoryStore(), publishers, CREDS, **defaults)


def state_of(client, post_id: str, platform: str) -> str:
    return state_column(client, post_id, platform, "State")


# ==========================================================================
# The ten scenarios from the brief
# ==========================================================================
class TestFailureInjection:
    def test_crash_before_the_api_request_is_retried_next_run(self):
        """A lease that was never acted on expires, reconciles, reschedules."""
        stale = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PUBLISHING, attempts=1,
            started_at=NOW - dt.timedelta(minutes=45),  # older than the lease
        )
        client = build([post_row(Platforms="FB")], states=[stale])
        # The lookup works and finds nothing: evidence of absence, so it is
        # safe to schedule again rather than strand the post.
        publishers = {Platform.FB: FakePublisher(Platform.FB, recent=[])}
        result = run(client, publishers)

        assert "gi-0001/FB" in result.leases_expired
        assert state_of(client, "gi-0001", "FB") in {"published", "publishing", "scheduled"}
        assert result.reconciled.returned_to_scheduled or result.attempts

    def test_crash_after_2xx_before_state_write_is_never_resent(self):
        """The classic double-publish risk. It must resolve by looking."""
        unknown = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.UNKNOWN, attempts=1,
            started_at=NOW - dt.timedelta(minutes=5),
        )
        client = build([post_row(Platforms="FB")], states=[unknown])
        already_live = RemotePost(
            remote_id="fb-999",
            created_at=NOW - dt.timedelta(minutes=5),
            caption="Every love story deserves a beginning worth remembering.",
            url="https://facebook.com/fb-999",
        )
        publisher = FakePublisher(Platform.FB, recent=[already_live])
        result = run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "published"
        assert state_column(client, "gi-0001", "FB", "Remote ID") == "fb-999"
        # THE assertion: it was found, so it was never sent again.
        assert publisher.call_count == 0
        assert result.reconciled.resolved_published == [("gi-0001", Platform.FB)]

    def test_unknown_stays_for_a_human_when_the_lookup_itself_fails(self):
        """A failed lookup is not evidence of absence."""
        unknown = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.UNKNOWN, attempts=1, started_at=NOW - dt.timedelta(minutes=5),
        )
        client = build([post_row(Platforms="FB")], states=[unknown])
        publisher = FakePublisher(Platform.FB)
        publisher.lookup_error = ConnectionError("platform unreachable")
        run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "unknown"
        assert publisher.call_count == 0
        assert client.cell("grandinvitation", 2, "Status") == RowStatus.NEEDS_REVIEW.value

    def test_sheet_write_failure_after_success_is_covered_by_the_lease(self):
        """FB succeeds, then recording it explodes.

        Nothing can be written — the Sheet is the thing that is broken — so the
        recovery is the lease itself: it was written BEFORE the API call, so
        `_State` still says `publishing`. That row ages out into `unknown` and
        reconciliation settles it by looking at the platform. This is precisely
        why the lease is written first, and it is why the run is allowed to die
        here rather than trying to paper over it.
        """
        client = build([post_row(Platforms="FB, IG")])
        fb = FakePublisher(Platform.FB, PublishResult.success("fb-1", "https://fb/1"))
        ig = FakePublisher(Platform.IG, PublishResult.success("ig-1", "https://ig/1"))

        original = client.replace_rows

        def explode_once_fb_has_been_sent(title, headers, rows):
            # Fail the first _State write that happens after FB was published.
            if title == "_State" and fb.call_count == 1:
                raise RuntimeError("Sheets API 503")
            original(title, headers, rows)

        client.replace_rows = explode_once_fb_has_been_sent

        with pytest.raises(RuntimeError):
            run(client, {Platform.FB: fb, Platform.IG: ig})

        # FB was sent exactly once — the failure was in recording, not sending.
        assert fb.call_count == 1
        # IG never ran, so it is untouched and will be picked up next time:
        # the two platforms are genuinely independent jobs.
        assert ig.call_count == 0
        # The lease survives in the Sheet, which is what makes this recoverable.
        assert state_of(client, "gi-0001", "FB") == "publishing"
        assert state_of(client, "gi-0001", "IG") == "scheduled"

    def test_the_surviving_lease_then_resolves_to_published_next_run(self):
        """Continues the scenario above: the next run finds the post live."""
        leased = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PUBLISHING, attempts=1,
            started_at=NOW - dt.timedelta(minutes=40),
        )
        client = build([post_row(Platforms="FB")], states=[leased])
        live = RemotePost(
            remote_id="fb-1",
            created_at=NOW - dt.timedelta(minutes=40),
            caption="Every love story deserves a beginning worth remembering.",
            url="https://facebook.com/fb-1",
        )
        publisher = FakePublisher(Platform.FB, recent=[live])
        result = run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "published"
        assert ("gi-0001", Platform.FB) in result.reconciled.resolved_published
        # Never re-sent, which is the entire point.
        assert publisher.call_count == 0

    def test_ig_succeeds_li_gets_429_row_is_partial_and_li_backs_off(self):
        client = build([post_row(Platforms="IG, LI")])
        ig = FakePublisher(Platform.IG, PublishResult.success("ig-1", "https://ig/1"))
        li = FakePublisher(Platform.LI, PublishResult.retryable("HTTP 429 rate limited"))
        run(client, {Platform.IG: ig, Platform.LI: li})

        assert state_of(client, "gi-0001", "IG") == "published"
        assert state_of(client, "gi-0001", "LI") == "retryable_failed"
        assert client.cell("grandinvitation", 2, "Status") == RowStatus.PARTIAL.value
        # Backoff is scheduled, not immediate.
        assert state_column(client, "gi-0001", "LI", "Next Attempt At") != ""

    def test_runner_dies_during_ffmpeg_so_nothing_reached_the_platform(self):
        """Lease expires, the lookup works, nothing is found, back to scheduled."""
        stale = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PUBLISHING, attempts=1,
            started_at=NOW - dt.timedelta(minutes=30),
        )
        client = build([post_row(Platforms="FB")], states=[stale])
        publisher = FakePublisher(Platform.FB, recent=[])
        result = run(client, {Platform.FB: publisher})

        assert "gi-0001/FB" in result.leases_expired
        assert ("gi-0001", Platform.FB) in result.reconciled.returned_to_scheduled

    def test_expired_token_is_permanent_with_a_clear_message(self):
        client = build([post_row(Platforms="FB")])
        publisher = FakePublisher(
            Platform.FB, PublishResult.permanent("OAuthException: Session has expired")
        )
        run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "permanent_failed"
        assert "expired" in state_column(client, "gi-0001", "FB", "Last Error")
        # No free retries on a 4xx.
        assert publisher.call_count == 1

    def test_a_second_concurrent_run_skips_leased_rows(self):
        fresh = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PUBLISHING, attempts=1,
            started_at=NOW - dt.timedelta(minutes=2),  # well inside the lease
        )
        client = build([post_row(Platforms="FB")], states=[fresh])
        publisher = FakePublisher(Platform.FB)
        result = run(client, {Platform.FB: publisher})

        assert "gi-0001/FB" in result.leases_skipped
        # The other run owns it; touching it could publish twice.
        assert publisher.call_count == 0
        assert state_of(client, "gi-0001", "FB") == "publishing"

    def test_editing_a_caption_after_publishing_warns_and_never_republishes(self):
        published = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PUBLISHED, attempts=1, content_hash="staleHash",
            remote_url="https://facebook.com/1",
        )
        client = build([post_row(Platforms="FB", Caption="EDITED AFTER PUBLISHING")], states=[published])
        publisher = FakePublisher(Platform.FB)
        run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "published"
        assert publisher.call_count == 0
        assert "differs from the Sheet" in client.cell("grandinvitation", 2, "Notes")

    def test_fixing_invalid_media_reschedules_automatically(self):
        invalid = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.INVALID, content_hash="oldHash", last_error="no file named 'typo.png'",
        )
        client = build([post_row(Platforms="FB", Media="a.png")], states=[invalid])
        publisher = FakePublisher(Platform.FB, PublishResult.success("fb-1", "https://fb/1"))
        run(client, {Platform.FB: publisher})

        # Corrected rows retry themselves, with no terminal involved.
        assert state_of(client, "gi-0001", "FB") == "published"

    def test_action_retry_resets_attempts_and_clears_the_cell(self):
        failed = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PERMANENT_FAILED, attempts=3, last_error="boom",
        )
        client = build([post_row(Platforms="FB", Action="retry")], states=[failed])
        publisher = FakePublisher(Platform.FB, PublishResult.success("fb-2", "https://fb/2"))
        result = run(client, {Platform.FB: publisher})

        assert "gi-0001: retry" in result.actions_applied
        assert state_of(client, "gi-0001", "FB") == "published"
        # A blank Action is how the teammate knows it was handled.
        assert client.cell("grandinvitation", 2, "Action") == ""


# ==========================================================================
# The machinery underneath
# ==========================================================================
class TestActions:
    def test_mark_published_records_a_manual_url(self):
        failed = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.UNKNOWN, attempts=1,
        )
        client = build([post_row(Platforms="FB", Action="mark published")], states=[failed])
        publisher = FakePublisher(Platform.FB)
        run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "published"
        assert state_column(client, "gi-0001", "FB", "Remote URL") == "manual"
        assert publisher.call_count == 0

    def test_skip_stops_the_post_without_failing_it(self):
        client = build([post_row(Platforms="FB", Action="skip")])
        publisher = FakePublisher(Platform.FB)
        run(client, {Platform.FB: publisher})

        assert state_of(client, "gi-0001", "FB") == "skipped"
        assert publisher.call_count == 0

    def test_actions_apply_to_every_platform_of_the_row(self):
        client = build([post_row(Platforms="FB, IG", Action="skip")])
        run(client, {Platform.FB: FakePublisher(Platform.FB), Platform.IG: FakePublisher(Platform.IG)})
        assert state_of(client, "gi-0001", "FB") == "skipped"
        assert state_of(client, "gi-0001", "IG") == "skipped"


class TestSelection:
    def _result(self, rows, states=None):
        from postpilot.sync import sync

        client = build(rows, states=states)
        return client, sync(client, tz_name=TZ, now=NOW, write=False, drive=drive())

    def test_future_posts_are_not_due(self):
        _, result = self._result([post_row(Date="2026-12-25", Platforms="FB")])
        due, _, _ = select_due(result, now=NOW, lease_minutes=20)
        assert due == []

    def test_past_posts_are_due(self):
        _, result = self._result([post_row(Platforms="FB")])
        due, _, _ = select_due(result, now=NOW, lease_minutes=20)
        assert [d[2].platform for d in due] == [Platform.FB]

    def test_backoff_is_respected(self):
        waiting = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.RETRYABLE_FAILED, attempts=1,
            next_attempt_at=NOW + dt.timedelta(minutes=10),
        )
        _, result = self._result([post_row(Platforms="FB")], states=[waiting])
        due, _, _ = select_due(result, now=NOW, lease_minutes=20)
        assert due == []

    def test_published_platforms_are_never_selected(self):
        done = StateRow(
            post_id="gi-0001", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.PUBLISHED, attempts=1,
        )
        _, result = self._result([post_row(Platforms="FB")], states=[done])
        due, _, _ = select_due(result, now=NOW, lease_minutes=20)
        assert due == []

    def test_invalid_platforms_are_not_selected(self):
        # text + IG is invalid for IG only; FB stays due.
        _, result = self._result([post_row(Platforms="FB, IG", Type="text", Media="")])
        due, _, _ = select_due(result, now=NOW, lease_minutes=20)
        assert [d[2].platform for d in due] == [Platform.FB]


class TestRecordResult:
    def _state(self, attempts: int = 1) -> StateRow:
        return StateRow(
            post_id="p", brand_slug="b", platform=Platform.FB,
            state=PlatformState.PUBLISHING, attempts=attempts, attempt_id="abc",
        )

    def test_success_clears_the_lease_and_records_the_remote_id(self):
        state = self._state()
        record_result(state, PublishResult.success("r1", "https://x/1"), now=NOW, max_attempts=3, backoff_base=300)
        assert state.state is PlatformState.PUBLISHED
        assert state.remote_id == "r1" and state.completed_at == NOW
        assert state.attempt_id == ""

    def test_retryable_backs_off_and_keeps_trying(self):
        state = self._state(attempts=1)
        record_result(state, PublishResult.retryable("429"), now=NOW, max_attempts=3, backoff_base=300)
        assert state.state is PlatformState.RETRYABLE_FAILED
        assert state.next_attempt_at == NOW + dt.timedelta(seconds=300)

    def test_retryable_becomes_permanent_once_attempts_run_out(self):
        state = self._state(attempts=3)
        record_result(state, PublishResult.retryable("429"), now=NOW, max_attempts=3, backoff_base=300)
        assert state.state is PlatformState.PERMANENT_FAILED
        assert "gave up after 3" in state.last_error

    def test_permanent_never_schedules_a_retry(self):
        state = self._state()
        record_result(state, PublishResult.permanent("400 bad request"), now=NOW, max_attempts=3, backoff_base=300)
        assert state.state is PlatformState.PERMANENT_FAILED
        assert state.next_attempt_at is None

    def test_unknown_keeps_the_container_id_for_reconciliation(self):
        state = self._state()
        record_result(
            state, PublishResult.unknown("timeout after send", container_id="container-42"),
            now=NOW, max_attempts=3, backoff_base=300,
        )
        assert state.state is PlatformState.UNKNOWN
        assert state.remote_id == "container-42"
        assert state.next_attempt_at is None

    def test_backoff_grows_and_is_capped(self):
        assert backoff_delay(1, 300) == dt.timedelta(seconds=300)
        assert backoff_delay(2, 300) == dt.timedelta(seconds=600)
        assert backoff_delay(3, 300) == dt.timedelta(seconds=1200)
        assert backoff_delay(20, 300) == dt.timedelta(hours=6)


class TestAdapterSafety:
    def test_an_adapter_that_raises_becomes_unknown_not_a_retry(self):
        client = build([post_row(Platforms="FB")])
        publisher = FakePublisher(Platform.FB, RuntimeError("something odd"))
        run(client, {Platform.FB: publisher})
        # We cannot tell whether the request was sent, so we must not resend.
        assert state_of(client, "gi-0001", "FB") == "unknown"

    def test_a_missing_adapter_fails_permanently_rather_than_looping(self):
        client = build([post_row(Platforms="FB, LI")])
        run(client, {Platform.FB: FakePublisher(Platform.FB)})
        assert state_of(client, "gi-0001", "LI") == "permanent_failed"
        assert "adapter" in state_column(client, "gi-0001", "LI", "Last Error")

    def test_media_problems_are_permanent_not_retryable(self):
        client = build([post_row(Platforms="FB", Media="ghost.png")])
        publisher = FakePublisher(Platform.FB)
        run(client, {Platform.FB: publisher})
        # A missing file is not fixed by waiting; it is fixed by a human.
        assert state_of(client, "gi-0001", "FB") in {"invalid", "permanent_failed"}
        assert publisher.call_count == 0


class TestDryRun:
    def test_dry_run_writes_no_lease_and_sends_nothing(self):
        client = build([post_row(Platforms="FB, IG")])
        fb = FakePublisher(Platform.FB)
        ig = FakePublisher(Platform.IG)
        result = run(client, {Platform.FB: fb, Platform.IG: ig}, dry_run=True)

        assert fb.call_count == 0 and ig.call_count == 0
        assert all(a.dry_run for a in result.attempts)
        assert len(result.attempts) == 2
        # No lease was written, so a real run afterwards is unaffected.
        assert client.replaced == {}

    def test_dry_run_still_reports_what_would_happen(self):
        client = build([post_row(Platforms="FB")])
        result = run(client, {Platform.FB: FakePublisher(Platform.FB)}, dry_run=True)
        assert result.attempts[0].detail == "would publish"
