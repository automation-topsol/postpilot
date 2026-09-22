"""The daily digest — the only routine signal that the scheduler is alive.

Because that is what it is for, it must never be *silently* skipped. Telegram
is optional by design (CLAUDE.md §0.5), so when it is unconfigured the same
digest is written to the `_Log` tab instead, where it is durable, timestamped
and visible to the teammate who already lives in the Sheet.

One message a day at 09:00 Asia/Karachi. No per-post pings — a notification
that fires on every success is a notification nobody reads.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import httpx

from postpilot.logging import get_logger
from postpilot.models import LogEntry, Platform, PlatformState, RowStatus
from postpilot.sync import SyncResult

log = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org"
UPCOMING_HOURS = 24
TOKEN_WARN_DAYS = 7


@dataclass
class BrandDigest:
    slug: str
    name: str
    published: int = 0
    failed: int = 0
    needs_review: int = 0
    invalid: int = 0
    upcoming: int = 0

    @property
    def quiet(self) -> bool:
        return not any((self.published, self.failed, self.needs_review, self.invalid, self.upcoming))


@dataclass
class Digest:
    generated_at: dt.datetime
    brands: list[BrandDigest] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def needs_attention(self) -> int:
        return sum(b.failed + b.needs_review + b.invalid for b in self.brands)

    @property
    def total_published(self) -> int:
        return sum(b.published for b in self.brands)


def build_digest(
    result: SyncResult,
    settings,
    *,
    now: dt.datetime | None = None,
    since_hours: int = 24,
) -> Digest:
    now = now or dt.datetime.now(dt.UTC)
    since = now - dt.timedelta(hours=since_hours)
    cutoff = now + dt.timedelta(hours=UPCOMING_HOURS)
    digest = Digest(generated_at=now)

    for brand_sync in result.brands:
        entry = BrandDigest(slug=brand_sync.brand.slug, name=brand_sync.brand.name)

        for post in brand_sync.posts:
            status = brand_sync.statuses.get(post.post_id)
            if status is RowStatus.NEEDS_REVIEW:
                entry.needs_review += 1
            elif status is RowStatus.FAILED:
                entry.failed += 1
            elif status is RowStatus.INVALID:
                entry.invalid += 1
            if post.scheduled_at and now <= post.scheduled_at <= cutoff:
                entry.upcoming += 1

        for platform in Platform:
            for state in result.states.values():
                if state.brand_slug != brand_sync.brand.slug or state.platform is not platform:
                    continue
                # "Published" means published *in this window*, not ever.
                if state.state is PlatformState.PUBLISHED and state.completed_at and state.completed_at >= since:
                    entry.published += 1

        digest.brands.append(entry)

    digest.warnings.extend(_token_warnings(result, settings, now))
    return digest


def _token_warnings(result: SyncResult, settings, now: dt.datetime) -> list[str]:
    """Expiry warnings. A token dying unnoticed is the worst failure we have."""
    warnings: list[str] = []

    expires = settings.linkedin_expires_at
    if settings.has_linkedin and expires is not None:
        days = (expires - now).days
        if days <= 0:
            warnings.append("LinkedIn token has EXPIRED — run `postpilot auth linkedin --refresh`")
        elif days <= TOKEN_WARN_DAYS:
            warnings.append(f"LinkedIn token expires in {days} day(s) — refresh it")

    for brand_sync in result.brands:
        brand = brand_sync.brand
        needs_meta = {Platform.FB, Platform.IG} & set(brand.enabled_platforms)
        if needs_meta and not settings.has_meta_token(brand.slug):
            warnings.append(f"{brand.slug}: {brand.token_env_var} is not set")
        if Platform.LI in brand.enabled_platforms and not settings.has_linkedin:
            warnings.append(f"{brand.slug}: LinkedIn is enabled but LINKEDIN_ACCESS_TOKEN is not set")

    warnings.extend(result.warnings)
    return warnings


def render_text(digest: Digest, tz_name: str) -> str:
    """Plain text, readable in Telegram and in a spreadsheet cell alike."""
    from zoneinfo import ZoneInfo

    local = digest.generated_at.astimezone(ZoneInfo(tz_name))
    lines = [f"PostPilot — {local:%a %d %b, %H:%M} ({tz_name})"]

    active = [b for b in digest.brands if not b.quiet]
    if not active:
        lines.append("")
        lines.append("Nothing scheduled and nothing to fix. All quiet.")
    for brand in active:
        lines.append("")
        lines.append(f"{brand.name}")
        bits = []
        if brand.published:
            bits.append(f"{brand.published} published")
        if brand.upcoming:
            bits.append(f"{brand.upcoming} due in 24h")
        if brand.failed:
            bits.append(f"{brand.failed} FAILED")
        if brand.needs_review:
            bits.append(f"{brand.needs_review} NEEDS REVIEW")
        if brand.invalid:
            bits.append(f"{brand.invalid} invalid")
        lines.append("  " + ", ".join(bits))

    if digest.needs_attention:
        lines.append("")
        lines.append(
            f"{digest.needs_attention} row(s) need a person. Open the Sheet, read the Error "
            "column, and fix the row or set Action."
        )

    if digest.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in dict.fromkeys(digest.warnings))

    return "\n".join(lines)


def send(digest: Digest, settings, client=None, *, tz_name: str = "Asia/Karachi") -> tuple[bool, str]:
    """Deliver via Telegram. Returns (delivered, detail).

    Never raises: a notifier that crashes the run would turn a reporting
    problem into a delivery problem.
    """
    text = render_text(digest, tz_name)
    credentials = settings.telegram
    if credentials is None:
        return False, "Telegram is not configured"

    token, chat_id = credentials
    try:
        response = (client or httpx).post(
            f"{TELEGRAM_API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        body = response.json()
    except Exception as exc:
        return False, f"Telegram send failed: {exc}"

    if body.get("ok"):
        return True, f"delivered to {chat_id}"
    return False, f"Telegram refused: {body.get('description', '')}"


def to_log_entries(digest: Digest, tz_name: str, *, reason: str) -> list[LogEntry]:
    """The digest as `_Log` rows — the fallback when Telegram cannot deliver.

    Written line by line rather than as one blob so it stays readable in a
    spreadsheet cell.
    """
    text = render_text(digest, tz_name)
    return [
        LogEntry(
            timestamp=digest.generated_at,
            action="summary",
            result="logged" if reason == "unconfigured" else "fallback",
            details=line.strip(),
        )
        for line in text.splitlines()
        if line.strip()
    ]
