"""Regression tests for bugs found by adversarial probing.

Each of these was a real defect in committed code, not a hypothetical. The
comments say what the wrong behaviour was, because a test that only asserts the
right answer does not stop someone "simplifying" their way back to the bug.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from PIL import Image

from postpilot.media.normalise import normalise_image
from postpilot.media.policies import InstagramImagePolicy
from postpilot.models import (
    Brand,
    Platform,
    PlatformState,
    RowStatus,
    StateRow,
    roll_up_status,
)
from postpilot.publishers.base import BrandCreds, RemotePost
from postpilot.reconcile import reconcile
from postpilot.sheets.schema import BRANDS_HEADERS, LOG_HEADERS, STATE_HEADERS, brand_headers
from postpilot.sync import sync
from tests.conftest import TZ
from tests.fakes import FakeSheetClient, state_column

NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)
H = brand_headers()


def _state(platform: Platform, state: PlatformState) -> StateRow:
    return StateRow(post_id="p", brand_slug="b", platform=platform, state=state)


class TestRollUpPublishedPlusBlocked:
    """WAS: a row with one platform published and another invalid reported
    `scheduled` — which reads as "nothing wrong, it is queued", when in fact
    half of it is live and the other half can never go."""

    def test_published_plus_invalid_is_partial(self):
        states = [_state(Platform.FB, PlatformState.PUBLISHED),
                  _state(Platform.IG, PlatformState.INVALID)]
        assert roll_up_status(states, is_draft=False, has_issues=True) is RowStatus.PARTIAL

    def test_published_plus_permanent_failure_is_partial(self):
        states = [_state(Platform.FB, PlatformState.PUBLISHED),
                  _state(Platform.IG, PlatformState.PERMANENT_FAILED)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.PARTIAL

    def test_published_plus_skipped_plus_invalid_is_partial(self):
        states = [_state(Platform.FB, PlatformState.PUBLISHED),
                  _state(Platform.IG, PlatformState.SKIPPED),
                  _state(Platform.LI, PlatformState.INVALID)]
        assert roll_up_status(states, is_draft=False, has_issues=True) is RowStatus.PARTIAL

    def test_scheduled_plus_invalid_is_still_scheduled(self):
        # Nothing is live yet and something can still go out, so `scheduled`
        # remains the honest summary.
        states = [_state(Platform.FB, PlatformState.SCHEDULED),
                  _state(Platform.IG, PlatformState.INVALID)]
        assert roll_up_status(states, is_draft=False, has_issues=True) is RowStatus.SCHEDULED

    def test_published_plus_still_scheduled_is_not_partial(self):
        # `partial` means "some of it cannot go". A platform simply not having
        # run yet is not a problem and must not look like one.
        states = [_state(Platform.FB, PlatformState.PUBLISHED),
                  _state(Platform.IG, PlatformState.SCHEDULED)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.SCHEDULED

    def test_everything_skipped_does_not_report_failure(self):
        states = [_state(Platform.FB, PlatformState.SKIPPED),
                  _state(Platform.IG, PlatformState.SKIPPED)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.SCHEDULED


class TestReconcileScope:
    """WAS: a `--brand X` run reconciled EVERY brand's `unknown` rows. Brand
    Y's captions were never loaded, so the match always failed, so a post that
    had genuinely published was returned to `scheduled` — and republished on
    the next full run. A double publish, straight through the guarantee."""

    @staticmethod
    def _publisher(recent):
        class Pub:
            platform = Platform.FB

            def __init__(self):
                self.lookups = 0

            def publish(self, *a):
                raise AssertionError("reconcile must never publish")

            def find_recent(self, creds, since):
                self.lookups += 1
                return list(recent)

        return Pub()

    def _unknown(self, slug="brandb", post_id="bb-0001") -> StateRow:
        return StateRow(
            post_id=post_id, brand_slug=slug, platform=Platform.FB,
            state=PlatformState.UNKNOWN, attempts=1, started_at=NOW - dt.timedelta(minutes=5),
        )

    def _creds(self, slug="brandb"):
        brand = Brand(name="B", slug=slug, enabled_platforms=[Platform.FB], facebook_page_id="2")
        return {slug: BrandCreds(brand=brand, meta_page_token="t")}

    def test_a_state_outside_this_runs_scope_is_left_untouched(self):
        state = self._unknown()
        live = RemotePost(remote_id="fb-1", created_at=NOW - dt.timedelta(minutes=5),
                          caption="brand B's real caption", url="u")
        publisher = self._publisher([live])

        # captions is empty: this run synced a different brand entirely.
        outcome = reconcile([state], {Platform.FB: publisher}, self._creds(), {}, now=NOW)

        assert state.state is PlatformState.UNKNOWN
        assert outcome.returned_to_scheduled == []
        # It must not even look: a lookup it cannot interpret is wasted quota.
        assert publisher.lookups == 0

    def test_a_state_inside_scope_still_reconciles(self):
        state = self._unknown()
        live = RemotePost(remote_id="fb-1", created_at=NOW - dt.timedelta(minutes=5),
                          caption="brand B's real caption", url="u")
        publisher = self._publisher([live])
        captions = {("bb-0001", Platform.FB): "brand B's real caption"}

        reconcile([state], {Platform.FB: publisher}, self._creds(), captions, now=NOW)
        assert state.state is PlatformState.PUBLISHED

    def test_a_post_with_no_caption_is_left_for_a_human(self):
        # Matching is caption + time. With no caption a miss proves nothing,
        # so rescheduling could publish twice.
        state = self._unknown()
        publisher = self._publisher([])
        captions = {("bb-0001", Platform.FB): "   "}

        outcome = reconcile([state], {Platform.FB: publisher}, self._creds(), captions, now=NOW)
        assert state.state is PlatformState.UNKNOWN
        assert outcome.still_unknown == [("bb-0001", Platform.FB)]
        assert "no caption" in state.last_error


class TestExifOrientation:
    """WAS: EXIF rotation was ignored. Phone cameras store orientation as a
    tag rather than rotating pixels, so portrait photos published sideways —
    and the aspect logic padded the wrong axis on the way."""

    @staticmethod
    def _tagged(width: int, height: int, orientation: int) -> bytes:
        buffer = io.BytesIO()
        exif = Image.Exif()
        exif[274] = orientation
        Image.new("RGB", (width, height), (30, 90, 160)).save(buffer, "JPEG", exif=exif)
        return buffer.getvalue()

    def test_rotate_90_is_applied(self):
        # Stored 1000x600 but tagged "rotate 90": it should be treated as tall.
        out = normalise_image(self._tagged(1000, 600, 6), InstagramImagePolicy())
        assert out.height > out.width

    def test_rotate_270_is_applied(self):
        out = normalise_image(self._tagged(1000, 600, 8), InstagramImagePolicy())
        assert out.height > out.width

    def test_untagged_images_are_unaffected(self):
        buffer = io.BytesIO()
        Image.new("RGB", (1000, 600), (30, 90, 160)).save(buffer, "JPEG")
        out = normalise_image(buffer.getvalue(), InstagramImagePolicy())
        assert out.width > out.height

    @pytest.mark.parametrize("mode", ["L", "LA", "P", "CMYK", "RGBA"])
    def test_every_colour_mode_survives(self, mode):
        buffer = io.BytesIO()
        colour = {"L": 128, "LA": (128, 255), "P": 1, "CMYK": (0, 0, 0, 0), "RGBA": (1, 2, 3, 255)}[mode]
        fmt = "JPEG" if mode == "CMYK" else "PNG"
        Image.new(mode, (800, 800), colour).save(buffer, fmt)
        assert normalise_image(buffer.getvalue(), InstagramImagePolicy()).data


class TestDuplicatePostIds:
    """WAS: two rows sharing an ID were accepted. They then shared `_State`,
    so publishing one marked the other published and it never went out.
    Copying a row is the most natural thing a teammate does."""

    @staticmethod
    def _client(rows):
        brand_row = [""] * len(BRANDS_HEADERS)
        for key, value in (("Brand Name", "Grand Invitation"), ("Slug", "gi"),
                           ("Enabled Platforms", "FB"), ("Facebook Page ID", "1"), ("Active", "TRUE")):
            brand_row[BRANDS_HEADERS.index(key)] = value
        return FakeSheetClient({
            "_Brands": (BRANDS_HEADERS, [brand_row]),
            "_State": (STATE_HEADERS, []),
            "_Log": (LOG_HEADERS, []),
            "gi": (H, rows),
        })

    @staticmethod
    def _row(**kw):
        row = [""] * len(H)
        for key, value in kw.items():
            row[H.index(key)] = value
        return row

    def test_duplicate_ids_stop_both_rows(self):
        client = self._client([
            self._row(ID="gi-0001", Date="2026-10-07", Platforms="FB", Type="image",
                      Media="a.png", Caption="original"),
            self._row(ID="gi-0001", Date="2026-10-08", Platforms="FB", Type="image",
                      Media="b.png", Caption="pasted copy"),
        ])
        sync(client, tz_name=TZ)

        assert client.cell("gi", 2, "Status") == RowStatus.INVALID.value
        assert client.cell("gi", 3, "Status") == RowStatus.INVALID.value
        assert "duplicate ID" in client.cell("gi", 2, "Error")
        # The fix is one keystroke, and the message says which one.
        assert "Clear the ID cell" in client.cell("gi", 2, "Error")
        assert state_column(client, "gi-0001", "FB", "State") == "invalid"

    def test_clearing_the_copied_id_resolves_it(self):
        client = self._client([
            self._row(ID="gi-0001", Date="2026-10-07", Platforms="FB", Type="image",
                      Media="a.png", Caption="original"),
            self._row(Date="2026-10-08", Platforms="FB", Type="image",
                      Media="b.png", Caption="pasted copy, id cleared"),
        ])
        sync(client, tz_name=TZ)
        assert client.cell("gi", 2, "Status") == RowStatus.SCHEDULED.value
        assert client.cell("gi", 3, "ID") == "gi-0002"
        assert client.cell("gi", 3, "Status") == RowStatus.SCHEDULED.value

    def test_unique_ids_are_unaffected(self):
        client = self._client([
            self._row(ID="gi-0001", Date="2026-10-07", Platforms="FB", Type="image",
                      Media="a.png", Caption="one"),
            self._row(ID="gi-0002", Date="2026-10-08", Platforms="FB", Type="image",
                      Media="b.png", Caption="two"),
        ])
        sync(client, tz_name=TZ)
        assert client.cell("gi", 2, "Status") == RowStatus.SCHEDULED.value
        assert client.cell("gi", 3, "Status") == RowStatus.SCHEDULED.value


class TestStateWriteAmplification:
    """WAS: every `_State` rewrite re-read the whole tab first, just to learn
    its previous extent. `publish` rewrites it several times per run, so the
    Sheets read quota was being spent on information already in memory."""

    @staticmethod
    def _client():
        from postpilot.sheets.client import SheetClient

        calls = {"reads": 0, "writes": 0}

        class FakeWs:
            def __init__(self, title, rows):
                self.title, self._rows, self.id = title, rows, 1

            def get_all_values(self):
                calls["reads"] += 1
                return self._rows

            def update(self, **kwargs):
                calls["writes"] += 1
                self._rows = kwargs.get("values", self._rows)

            def append_rows(self, *a, **kw):
                pass

            def row_values(self, n):
                return self._rows[0] if self._rows else []

        class FakeBook:
            title = "T"

            def __init__(self):
                self._ws = {"_State": FakeWs("_State", [STATE_HEADERS])}

            def worksheet(self, title):
                return self._ws.setdefault(title, FakeWs(title, [["a"]]))

            def worksheets(self):
                return list(self._ws.values())

        client = SheetClient.__new__(SheetClient)
        client._client = None
        client._sheet_id = "x"
        client._book = FakeBook()
        client._tabs = {}
        client._row_counts = {}
        return client, calls

    def test_repeated_rewrites_read_the_tab_once(self):
        client, calls = self._client()
        for _ in range(5):
            client.replace_rows("_State", STATE_HEADERS, [["r"] * len(STATE_HEADERS)])
        assert calls["writes"] == 5
        assert calls["reads"] <= 1

    def test_shrinking_still_clears_stale_rows(self):
        # The optimisation must not break the reason the read existed.
        client, _ = self._client()
        client.replace_rows("_State", STATE_HEADERS, [["a"] * 14, ["b"] * 14, ["c"] * 14])
        client.replace_rows("_State", STATE_HEADERS, [["a"] * 14])
        body = client._book._ws["_State"]._rows
        assert len(body) == 4           # header + 1 real + 2 blanking rows
        assert body[2][0] == ""
        assert body[3][0] == ""


class TestCaptionMatchPrecision:
    """WAS: matching compared only the first 60 characters. Two posts in a
    series ("…Part ONE", "…Part TWO") therefore looked identical, and a short
    caption like "Hi" matched anything beginning with it — so reconciliation
    could mark the WRONG post published, losing ours and recording a stranger's
    URL against it."""

    from postpilot.reconcile import captions_match as _match

    @pytest.mark.parametrize(
        ("ours", "theirs", "expected", "why"),
        [
            ("Hello world", "Hello world", True, "identical"),
            ("Hello  world\n\n", "hello world", True, "whitespace and case normalised"),
            (
                "A caption that is quite long and detailed about weddings",
                "A caption that is quite long and detailed about weddings #tag #more",
                True,
                "platform appended hashtags",
            ),
            (
                "A caption that is quite long and detailed about weddings",
                "A caption that is quite long and det",
                True,
                "platform truncated ours",
            ),
            (
                "Every love story deserves a beginning worth remembering. Part ONE of our series.",
                "Every love story deserves a beginning worth remembering. Part TWO of our series.",
                False,
                "a series sharing a long prefix must not collide",
            ),
            ("Hi", "Hi there, something else entirely", False, "short caption, loose match"),
            ("New post", "New post from a different campaign", False, "short-ish caption"),
            ("Completely different", "Nothing alike", False, "unrelated"),
            ("", "anything", False, "empty ours"),
            ("anything", "", False, "empty theirs"),
        ],
    )
    def test_matching(self, ours, theirs, expected, why):
        from postpilot.reconcile import captions_match

        assert captions_match(ours, theirs) is expected, why

    def test_a_short_caption_cannot_resolve_an_unknown(self):
        """End to end: the post stays for a human rather than being wrongly
        marked published against somebody else's post."""
        from postpilot.publishers.base import BrandCreds

        state = StateRow(
            post_id="p-1", brand_slug="b", platform=Platform.FB,
            state=PlatformState.UNKNOWN, attempts=1, started_at=NOW - dt.timedelta(minutes=5),
        )
        decoy = RemotePost(
            remote_id="someone-elses-post",
            created_at=NOW - dt.timedelta(minutes=4),
            caption="Hi everyone, here is a totally unrelated announcement",
        )

        class Pub:
            platform = Platform.FB

            def publish(self, *a):
                raise AssertionError("must not publish")

            def find_recent(self, creds, since):
                return [decoy]

        brand = Brand(name="B", slug="b", enabled_platforms=[Platform.FB], facebook_page_id="1")
        reconcile(
            [state], {Platform.FB: Pub()}, {"b": BrandCreds(brand=brand, meta_page_token="t")},
            {("p-1", Platform.FB): "Hi"}, now=NOW,
        )
        assert state.state is not PlatformState.PUBLISHED
        assert state.remote_id != "someone-elses-post"


class TestSkipMediaFootgun:
    """WAS: `sync --skip-media` hashed the raw Media text instead of the Drive
    fingerprint, so every stored hash differed and EVERY failed or invalid row
    across the Sheet was re-opened with its attempts reset. Harmless to
    published rows, but a nasty surprise from a flag that reads like a
    read-only convenience."""

    def test_the_two_modes_really_do_hash_differently(self):
        from postpilot.models import Post

        post = Post(post_id="p", brand_slug="b", row_number=2, media=["a.png"])
        assert post.content_hash() != post.content_hash("id-a:md5a")

    def test_cli_refuses_skip_media_without_dry_run(self):
        """The guard lives in the CLI, so assert the CLI enforces it."""
        from typer.testing import CliRunner

        from postpilot.cli import app

        result = CliRunner().invoke(app, ["sync", "--skip-media"])
        assert result.exit_code == 1
        assert "--dry-run" in result.output

    def test_cli_allows_skip_media_with_dry_run(self):
        from typer.testing import CliRunner

        from postpilot.cli import app

        result = CliRunner().invoke(app, ["sync", "--skip-media", "--dry-run"])
        # It may still fail on missing credentials in a bare environment; what
        # matters is that it is not the guard that stopped it.
        assert "--dry-run to inspect" not in result.output


class TestUnknownPostId:
    """WAS: `--post <typo>` reported "nothing to do" and exited 0, which is
    indistinguishable from success."""

    def test_prepare_rejects_an_unknown_id(self):
        from typer.testing import CliRunner

        from postpilot.cli import app

        result = CliRunner().invoke(app, ["prepare", "--post", "definitely-not-a-real-id"])
        assert result.exit_code == 1
        assert "no post with ID" in result.output

    def test_publish_rejects_an_unknown_id_before_leasing(self):
        from typer.testing import CliRunner

        from postpilot.cli import app

        result = CliRunner().invoke(app, ["publish", "--dry-run", "--post", "definitely-not-a-real-id"])
        assert result.exit_code == 1
        assert "no post with ID" in result.output
