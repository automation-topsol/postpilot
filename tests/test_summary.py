"""The daily digest, and its `_Log` fallback."""

from __future__ import annotations

import datetime as dt

import httpx
import respx

from postpilot.models import Platform, PlatformState, RowStatus, StateRow
from postpilot.summary import TELEGRAM_API, build_digest, render_text, send, to_log_entries
from postpilot.sync import BrandSync, SyncResult
from tests.test_publish import BRAND

NOW = dt.datetime(2026, 10, 7, 4, 0, tzinfo=dt.UTC)  # 09:00 Asia/Karachi
TZ = "Asia/Karachi"


class FakeSettings:
    def __init__(self, telegram=None, linkedin_expires=None, has_linkedin=False, meta=True) -> None:
        self.telegram = telegram
        self.linkedin_expires_at = linkedin_expires
        self.has_linkedin = has_linkedin
        self._meta = meta

    def has_meta_token(self, slug: str) -> bool:
        return self._meta


def make_result(*, published=0, failed=0, needs_review=0, upcoming=0) -> SyncResult:
    from postpilot.models import Post, PostType

    result = SyncResult()
    brand_sync = BrandSync(brand=BRAND)
    states: dict = {}
    index = 0

    def add(status: RowStatus, scheduled_at, state: PlatformState | None = None):
        nonlocal index
        index += 1
        post_id = f"gi-{index:04d}"
        post = Post(
            post_id=post_id, brand_slug=BRAND.slug, row_number=index + 1,
            platforms=[Platform.FB], post_type=PostType.IMAGE, scheduled_at=scheduled_at,
        )
        brand_sync.posts.append(post)
        brand_sync.statuses[post_id] = status
        if state is not None:
            states[(post_id, Platform.FB)] = StateRow(
                post_id=post_id, brand_slug=BRAND.slug, platform=Platform.FB,
                state=state, completed_at=NOW - dt.timedelta(hours=2),
            )

    for _ in range(published):
        add(RowStatus.PUBLISHED, NOW - dt.timedelta(hours=2), PlatformState.PUBLISHED)
    for _ in range(failed):
        add(RowStatus.FAILED, NOW - dt.timedelta(hours=3), PlatformState.PERMANENT_FAILED)
    for _ in range(needs_review):
        add(RowStatus.NEEDS_REVIEW, NOW - dt.timedelta(hours=1), PlatformState.UNKNOWN)
    for _ in range(upcoming):
        add(RowStatus.SCHEDULED, NOW + dt.timedelta(hours=5), PlatformState.SCHEDULED)

    result.brands.append(brand_sync)
    result.states = states
    return result


class TestDigest:
    def test_counts_by_category(self):
        digest = build_digest(make_result(published=2, failed=1, needs_review=1, upcoming=3),
                              FakeSettings(), now=NOW)
        brand = digest.brands[0]
        assert brand.published == 2 and brand.failed == 1
        assert brand.needs_review == 1 and brand.upcoming == 3
        assert digest.needs_attention == 2

    def test_only_counts_publishes_inside_the_window(self):
        result = make_result(published=1)
        # Completed a week ago: yesterday's digest already reported it.
        for state in result.states.values():
            state.completed_at = NOW - dt.timedelta(days=7)
        assert build_digest(result, FakeSettings(), now=NOW).brands[0].published == 0

    def test_quiet_day_says_so_rather_than_printing_nothing(self):
        digest = build_digest(make_result(), FakeSettings(), now=NOW)
        assert "All quiet" in render_text(digest, TZ)

    def test_attention_line_tells_the_teammate_what_to_do(self):
        digest = build_digest(make_result(failed=2), FakeSettings(), now=NOW)
        text = render_text(digest, TZ)
        assert "2 row(s) need a person" in text
        assert "Error column" in text

    def test_local_time_is_used_in_the_header(self):
        digest = build_digest(make_result(), FakeSettings(), now=NOW)
        # 04:00 UTC is 09:00 in Karachi — the hour the digest is scheduled for.
        assert "09:00" in render_text(digest, TZ)


class TestWarnings:
    def test_expiring_linkedin_token_warns(self):
        settings = FakeSettings(
            has_linkedin=True, linkedin_expires=NOW + dt.timedelta(days=3)
        )
        digest = build_digest(make_result(), settings, now=NOW)
        assert any("expires in 3 day" in w for w in digest.warnings)

    def test_expired_linkedin_token_is_shouted_about(self):
        settings = FakeSettings(has_linkedin=True, linkedin_expires=NOW - dt.timedelta(days=1))
        digest = build_digest(make_result(), settings, now=NOW)
        assert any("EXPIRED" in w for w in digest.warnings)

    def test_healthy_token_produces_no_noise(self):
        settings = FakeSettings(has_linkedin=True, linkedin_expires=NOW + dt.timedelta(days=45))
        assert build_digest(make_result(), settings, now=NOW).warnings == []

    def test_missing_meta_token_warns(self):
        digest = build_digest(make_result(), FakeSettings(meta=False), now=NOW)
        assert any("META_PAGE_TOKEN" in w for w in digest.warnings)


class TestDelivery:
    @respx.mock
    def test_telegram_success(self):
        respx.post(url__startswith=f"{TELEGRAM_API}/bot123/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        digest = build_digest(make_result(published=1), FakeSettings(), now=NOW)
        delivered, detail = send(digest, FakeSettings(telegram=("123", "-100999")), tz_name=TZ)
        assert delivered and "-100999" in detail

    def test_unconfigured_telegram_is_reported_not_raised(self):
        digest = build_digest(make_result(), FakeSettings(), now=NOW)
        delivered, detail = send(digest, FakeSettings(telegram=None), tz_name=TZ)
        assert delivered is False and "not configured" in detail

    @respx.mock
    def test_telegram_refusal_is_reported_not_raised(self):
        respx.post(url__startswith=f"{TELEGRAM_API}/bot123/sendMessage").mock(
            return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
        )
        digest = build_digest(make_result(), FakeSettings(), now=NOW)
        delivered, detail = send(digest, FakeSettings(telegram=("123", "bad")), tz_name=TZ)
        assert delivered is False and "chat not found" in detail

    @respx.mock
    def test_a_network_failure_never_raises(self):
        # A notifier that crashes the run turns a reporting problem into a
        # delivery problem.
        respx.post(url__startswith=f"{TELEGRAM_API}/bot123/sendMessage").mock(
            side_effect=httpx.ConnectError("down")
        )
        digest = build_digest(make_result(), FakeSettings(), now=NOW)
        delivered, detail = send(digest, FakeSettings(telegram=("123", "x")), tz_name=TZ)
        assert delivered is False and "failed" in detail


class TestLogFallback:
    def test_digest_becomes_log_rows(self):
        digest = build_digest(make_result(published=1, failed=1), FakeSettings(), now=NOW)
        entries = to_log_entries(digest, TZ, reason="unconfigured")
        assert entries and all(e.action == "summary" for e in entries)
        assert all(e.result == "logged" for e in entries)
        # One line per row keeps it readable in a spreadsheet cell.
        assert any("FAILED" in e.details for e in entries)

    def test_fallback_is_labelled_differently_from_unconfigured(self):
        digest = build_digest(make_result(), FakeSettings(), now=NOW)
        assert to_log_entries(digest, TZ, reason="fallback")[0].result == "fallback"

    def test_blank_lines_are_not_logged(self):
        digest = build_digest(make_result(published=1), FakeSettings(), now=NOW)
        assert all(e.details.strip() for e in to_log_entries(digest, TZ, reason="unconfigured"))
