"""The daily digest, and its `_Log` fallback."""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

import httpx
import pytest
import respx

from postpilot.models import Platform, PlatformState, RowStatus, StateRow
from postpilot.notify import TELEGRAM_API, EmailNotifier, SmtpConfig, TelegramNotifier
from postpilot.summary import build_digest, render_subject, render_text, send, to_log_entries
from postpilot.sync import BrandSync, SyncResult
from tests.test_publish import BRAND

NOW = dt.datetime(2026, 10, 7, 4, 0, tzinfo=dt.UTC)  # 09:00 Asia/Karachi
TZ = "Asia/Karachi"


class FakeSettings:
    def __init__(self, telegram=None, smtp=None, linkedin_expires=None, has_linkedin=False, meta=True) -> None:
        self.telegram = telegram
        self.smtp = smtp
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


SMTP = SmtpConfig(
    host="smtp.gmail.com", port=587, user="bot@example.com", password="apppassword1234",
    sender="bot@example.com", recipients=("a@example.com", "b@example.com"),
)


class FakeSMTP:
    """Stands in for smtplib.SMTP; records what would have gone out."""

    sent: ClassVar[list] = []
    fail_login: ClassVar[bool] = False
    fail_connect: ClassVar[bool] = False
    refuse: ClassVar[dict] = {}

    def __init__(self, host, port, timeout=None) -> None:
        if FakeSMTP.fail_connect:
            raise OSError("connection refused")
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        import smtplib

        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def send_message(self, message):
        FakeSMTP.sent.append(message)
        return dict(FakeSMTP.refuse)


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.sent, FakeSMTP.fail_login, FakeSMTP.fail_connect, FakeSMTP.refuse = [], False, False, {}


class OkNotifier:
    name = "ok"

    def send(self, subject, text):
        return True, "ok delivered"


class BrokenNotifier:
    name = "broken"

    def send(self, subject, text):
        raise RuntimeError("boom")


def digest_for(**kwargs):
    return build_digest(make_result(**kwargs), FakeSettings(), now=NOW)


class TestEmail:
    def test_sends_to_every_recipient_with_a_subject(self):
        notifier = EmailNotifier(SMTP, smtp_factory=FakeSMTP)
        delivered, detail = send(digest_for(failed=1), FakeSettings(), [notifier], tz_name=TZ)
        assert delivered and "a@example.com" in detail and "b@example.com" in detail
        message = FakeSMTP.sent[0]
        assert message["To"] == "a@example.com, b@example.com"
        assert "1 row(s) need attention" in message["Subject"]
        assert "FAILED" in message.get_content()

    def test_bad_app_password_is_reported_not_raised(self):
        FakeSMTP.fail_login = True
        delivered, detail = EmailNotifier(SMTP, smtp_factory=FakeSMTP).send("s", "t")
        assert delivered is False and "app password" in detail

    def test_unreachable_server_is_reported_not_raised(self):
        FakeSMTP.fail_connect = True
        delivered, detail = EmailNotifier(SMTP, smtp_factory=FakeSMTP).send("s", "t")
        assert delivered is False and "connection refused" in detail

    def test_partial_recipient_refusal_still_counts_as_delivered(self):
        FakeSMTP.refuse = {"b@example.com": (550, b"no such user")}
        delivered, detail = EmailNotifier(SMTP, smtp_factory=FakeSMTP).send("s", "t")
        assert delivered and "refused: b@example.com" in detail

    def test_every_recipient_refused_is_not_delivered(self):
        FakeSMTP.refuse = {r: (550, b"no") for r in SMTP.recipients}
        delivered, _ = EmailNotifier(SMTP, smtp_factory=FakeSMTP).send("s", "t")
        assert delivered is False

    def test_check_login_does_not_send(self):
        ok, _ = EmailNotifier(SMTP, smtp_factory=FakeSMTP).check_login()
        assert ok and FakeSMTP.sent == []


class TestSubject:
    def test_all_good(self):
        # The fixture brand enables LinkedIn, so a healthy token keeps it quiet.
        settings = FakeSettings(has_linkedin=True, linkedin_expires=NOW + dt.timedelta(days=45))
        digest = build_digest(make_result(published=1), settings, now=NOW)
        assert render_subject(digest, TZ).endswith("all good")

    def test_attention_leads(self):
        assert "2 row(s) need attention" in render_subject(digest_for(failed=2), TZ)

    def test_warnings_show_when_nothing_is_broken(self):
        digest = build_digest(make_result(), FakeSettings(meta=False), now=NOW)
        assert "warning(s)" in render_subject(digest, TZ)


class TestFanOut:
    def test_nothing_configured_is_reported_not_raised(self):
        delivered, detail = send(digest_for(), FakeSettings(), tz_name=TZ)
        assert delivered is False and "no notifier" in detail

    def test_one_success_is_enough(self):
        delivered, detail = send(digest_for(), FakeSettings(), [BrokenNotifier(), OkNotifier()], tz_name=TZ)
        assert delivered and "broken crashed" in detail and "ok delivered" in detail

    def test_a_notifier_that_raises_never_crashes_the_run(self):
        delivered, detail = send(digest_for(), FakeSettings(), [BrokenNotifier()], tz_name=TZ)
        assert delivered is False and "boom" in detail

    def test_settings_pick_up_both_notifiers(self):
        from postpilot.notify import configured_notifiers

        settings = FakeSettings(smtp=SMTP, telegram=("123", "-100"))
        assert [n.name for n in configured_notifiers(settings)] == ["email", "telegram"]


class TestSmtpSettings:
    def test_incomplete_smtp_is_none(self, monkeypatch):
        from postpilot.config import Settings

        monkeypatch.setenv("SMTP_USER", "bot@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
        monkeypatch.delenv("SUMMARY_TO", raising=False)
        assert Settings.model_construct().smtp is None

    def test_gmail_defaults_and_app_password_spaces(self, monkeypatch):
        from postpilot.config import Settings

        for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SMTP_USER", "bot@gmail.com")
        monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
        monkeypatch.setenv("SUMMARY_TO", " a@x.com, b@x.com ,")
        smtp = Settings.model_construct().smtp
        assert (smtp.host, smtp.port, smtp.sender) == ("smtp.gmail.com", 587, "bot@gmail.com")
        assert smtp.password == "abcdefghijklmnop"
        assert smtp.recipients == ("a@x.com", "b@x.com")


class TestTelegram:
    @respx.mock
    def test_telegram_success(self):
        respx.post(url__startswith=f"{TELEGRAM_API}/bot123/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        )
        delivered, detail = send(digest_for(published=1), FakeSettings(telegram=("123", "-100999")), tz_name=TZ)
        assert delivered and "-100999" in detail

    @respx.mock
    def test_telegram_refusal_is_reported_not_raised(self):
        respx.post(url__startswith=f"{TELEGRAM_API}/bot123/sendMessage").mock(
            return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
        )
        delivered, detail = TelegramNotifier("123", "bad").send("s", "t")
        assert delivered is False and "chat not found" in detail

    @respx.mock
    def test_a_network_failure_never_raises(self):
        # A notifier that crashes the run turns a reporting problem into a
        # delivery problem.
        respx.post(url__startswith=f"{TELEGRAM_API}/bot123/sendMessage").mock(
            side_effect=httpx.ConnectError("down")
        )
        delivered, detail = TelegramNotifier("123", "x").send("s", "t")
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
