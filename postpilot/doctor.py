"""`postpilot doctor` — check every credential and dependency, before it matters.

Design rules, learned the hard way during the Phase 0 spike:

- **A missing optional thing warns; a missing required thing fails.** Email
  and Telegram are optional by design, so its absence must never make a healthy install look
  broken.
- **Every failure names the fix**, not just the symptom. `AccessDenied` on a
  PUT means "the R2 token is read-only", not "check your credentials" — the
  credentials are fine, and sending someone to re-check them wastes an hour.
- **The public-read check is the important one.** Meta fetches media from its
  own servers, so an authenticated GET proves nothing.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass, field
from enum import StrEnum

from postpilot.apis import GRAPH_API_BASE
from postpilot.config import MissingSetting, Settings
from postpilot.media.normalise import FFMPEG, FFPROBE
from postpilot.models import Platform

PROBE_KEY = "postpilot/_doctor/probe.txt"
TOKEN_WARN_DAYS = 14


class Level(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Finding:
    level: Level
    name: str
    detail: str = ""
    fix: str = ""


@dataclass
class DoctorReport:
    findings: list[Finding] = field(default_factory=list)

    def add(self, level: Level, name: str, detail: str = "", fix: str = "") -> None:
        self.findings.append(Finding(level, name, detail, fix))

    def ok(self, name: str, detail: str = "") -> None:
        self.add(Level.PASS, name, detail)

    def warn(self, name: str, detail: str = "", fix: str = "") -> None:
        self.add(Level.WARN, name, detail, fix)

    def fail(self, name: str, detail: str = "", fix: str = "") -> None:
        self.add(Level.FAIL, name, detail, fix)

    @property
    def failed(self) -> bool:
        return any(f.level is Level.FAIL for f in self.findings)

    def counts(self) -> dict[Level, int]:
        return {level: sum(f.level is level for f in self.findings) for level in Level}


def run(settings: Settings, *, skip_network: bool = False) -> DoctorReport:
    report = DoctorReport()
    _check_tools(report)
    if skip_network:
        report.warn("network checks", "skipped (--offline)")
        return report

    brands = _check_google(report, settings)
    _check_r2(report, settings)
    _check_meta(report, settings, brands)
    _check_linkedin(report, settings, brands)
    _check_notifiers(report, settings)
    return report


# --------------------------------------------------------------------------
def _check_tools(report: DoctorReport) -> None:
    for binary in (FFMPEG, FFPROBE):
        if path := shutil.which(binary):
            report.ok(binary, path)
        else:
            report.fail(
                binary,
                "not on PATH — video normalisation will fail",
                fix="brew install ffmpeg (preinstalled on GitHub's ubuntu-latest)",
            )


def _check_google(report: DoctorReport, settings: Settings) -> list:
    from postpilot.sheets.client import SheetClient
    from postpilot.sheets.parse import parse_brands
    from postpilot.sheets.schema import BRANDS_TAB, LOG_TAB, STATE_TAB

    try:
        email = settings.service_account_email
        report.ok("service account", email)
    except MissingSetting as exc:
        report.fail("service account", str(exc), fix="set GOOGLE_SERVICE_ACCOUNT_JSON in .env")
        return []
    except Exception as exc:
        report.fail("service account", f"credentials will not parse: {exc}")
        return []

    try:
        client = SheetClient(settings.google_credentials, settings.sheet_id)
        titles = client.tab_titles()
        report.ok("Sheet readable", client.title)
    except MissingSetting as exc:
        report.fail("Sheet", str(exc))
        return []
    except Exception as exc:
        report.fail(
            "Sheet readable",
            str(exc)[:200],
            fix=f"share the Sheet with {email} as an Editor",
        )
        return []

    for required in (BRANDS_TAB, STATE_TAB, LOG_TAB):
        if required in titles:
            report.ok(f"tab {required}", "present")
        else:
            report.fail(f"tab {required}", "missing", fix="postpilot sheet init")

    brands_tab = client.read(BRANDS_TAB)
    if brands_tab is None:
        return []
    brands, problems, warnings = parse_brands(brands_tab.headers, brands_tab.rows)
    for problem in problems:
        report.fail("_Brands", problem)
    for warning in warnings:
        report.warn("_Brands", warning)
    if not brands:
        report.warn("_Brands", "no brands configured yet")
        return []
    report.ok("_Brands", f"{len(brands)} brand(s): {', '.join(b.slug for b in brands)}")

    for brand in brands:
        if brand.slug in titles:
            report.ok(f"tab {brand.slug}", "present")
        else:
            report.fail(f"tab {brand.slug}", "missing", fix="postpilot sheet init")

    _check_drive(report, settings, brands)
    return brands


def _check_drive(report: DoctorReport, settings: Settings, brands: list) -> None:
    from postpilot.drive import DriveClient

    drive = DriveClient(settings.google_credentials)
    for brand in brands:
        if not brand.drive_folder_id:
            report.warn(f"Drive [{brand.slug}]", "no Drive Folder ID in _Brands")
            continue
        try:
            files = drive.list_folder(brand.drive_folder_id)
        except Exception as exc:
            report.fail(
                f"Drive [{brand.slug}]",
                str(exc)[:160],
                fix=f"share the folder with {settings.service_account_email}",
            )
            continue

        usable = [f for f in files if (f.is_image or f.is_video) and f.md5]
        report.ok(f"Drive [{brand.slug}]", f"{len(files)} file(s), {len(usable)} usable")

        if missing := [f.name for f in files if (f.is_image or f.is_video) and not f.md5]:
            report.fail(
                f"Drive [{brand.slug}] checksums",
                f"missing on {', '.join(missing[:3])}",
                fix="re-upload those files; the R2 key depends on Drive's md5",
            )

        # Duplicate names are only a problem if a row references them, but
        # finding out now beats finding out when a post is due.
        seen: dict[str, int] = {}
        for item in files:
            seen[item.name.casefold()] = seen.get(item.name.casefold(), 0) + 1
        if dupes := [n for n, c in seen.items() if c > 1]:
            report.warn(
                f"Drive [{brand.slug}] duplicate names",
                ", ".join(dupes[:3]),
                fix="rename one of each pair — an ambiguous name is refused, never guessed",
            )


def _check_r2(report: DoctorReport, settings: Settings) -> None:
    import httpx

    from postpilot.media.store import R2Store

    try:
        store = R2Store.from_settings(settings)
    except MissingSetting as exc:
        report.fail("R2", str(exc))
        return

    base = settings.r2_public_base
    if not base.startswith("https://"):
        report.fail("R2 public URL", base, fix="Meta will not fetch media over plain http")
    elif ".r2.dev" in base:
        # The chosen configuration, not a misconfiguration — see CLAUDE.md §0.6.
        report.ok("R2 public URL", f"{base} (r2.dev development URL, ample at this volume)")
    else:
        report.ok("R2 public URL", base)

    body = f"postpilot doctor {dt.datetime.now(dt.UTC).isoformat()}\n".encode()
    try:
        store.put(PROBE_KEY, body, "text/plain; charset=utf-8")
        report.ok("R2 write", store.bucket)
    except Exception as exc:
        detail = str(exc)
        if "AccessDenied" in detail:
            report.fail(
                "R2 write",
                "AccessDenied — the bucket is reachable but this token cannot write",
                fix="create an R2 API token with 'Object Read & Write' on this bucket",
            )
        else:
            report.fail("R2 write", detail[:200])
        return

    try:
        # No credentials: exactly what Meta's fetcher does. An authenticated
        # GET would prove nothing about whether publishing will work.
        response = httpx.get(store.url_for(PROBE_KEY), timeout=20, follow_redirects=True)
        if response.status_code == 200 and response.content == body:
            report.ok("R2 public read", "anonymous fetch works — Meta will be able to read our media")
        else:
            report.fail(
                "R2 public read",
                f"HTTP {response.status_code}",
                fix="make the bucket publicly readable; publishing fetches media by URL",
            )
    except Exception as exc:
        report.fail("R2 public read", str(exc)[:200])
    finally:
        try:
            store.delete(PROBE_KEY)
        except Exception:
            report.warn("R2 cleanup", "could not delete the probe object")

    if confirmed := _env("R2_LIFECYCLE_CONFIRMED"):
        report.ok("R2 lifecycle", f"60-day expiry confirmed by hand on {confirmed}")
    else:
        report.warn(
            "R2 lifecycle",
            "cannot be read with an object-scoped token",
            fix="confirm the 60-day rule in the Cloudflare dashboard, then set R2_LIFECYCLE_CONFIRMED=<date>",
        )


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "").strip()


def _check_meta(report: DoctorReport, settings: Settings, brands: list) -> None:
    import httpx

    app_id = app_secret = ""
    try:
        app_id, app_secret = settings.meta_app
        report.ok("Meta app", f"app {app_id}")
    except MissingSetting:
        report.warn("Meta app", "META_APP_ID/META_APP_SECRET unset — token expiry cannot be checked")

    for brand in brands:
        needs_meta = {Platform.FB, Platform.IG} & set(brand.enabled_platforms)
        if not needs_meta:
            continue
        if not settings.has_meta_token(brand.slug):
            report.fail(
                f"Meta token [{brand.slug}]",
                f"{brand.token_env_var} is not set",
                fix="uv run python spike/meta_exchange_token.py --brand <slug> --write-env",
            )
            continue

        token = settings.meta_page_token(brand.slug)
        if app_id and app_secret:
            _check_meta_token(report, brand, token, app_id, app_secret)

        try:
            page = httpx.get(
                f"{GRAPH_API_BASE}/me", params={"fields": "id,name", "access_token": token}, timeout=20
            ).json()
        except Exception as exc:
            report.fail(f"Meta page [{brand.slug}]", str(exc)[:160])
            continue

        if "error" in page:
            report.fail(f"Meta page [{brand.slug}]", page["error"].get("message", "")[:160])
            continue

        if Platform.FB in brand.enabled_platforms:
            if str(page.get("id")) == brand.facebook_page_id:
                report.ok(f"Facebook [{brand.slug}]", f"{page.get('name')} ({page.get('id')})")
            else:
                # Publishing to the wrong Page is unrecoverable; v1 cannot delete.
                report.fail(
                    f"Facebook [{brand.slug}]",
                    f"_Brands says {brand.facebook_page_id}, the token belongs to {page.get('id')}",
                    fix="correct the Facebook Page ID in _Brands, or use the right token",
                )

        if Platform.IG in brand.enabled_platforms:
            _check_instagram(report, settings, brand, token, str(page.get("id", "")))


def _check_meta_token(report: DoctorReport, brand, token: str, app_id: str, app_secret: str) -> None:
    import httpx

    try:
        info = httpx.get(
            f"{GRAPH_API_BASE}/debug_token",
            params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
            timeout=20,
        ).json().get("data", {})
    except Exception as exc:
        report.warn(f"Meta token [{brand.slug}]", f"could not introspect: {exc}")
        return

    if not info.get("is_valid"):
        report.fail(f"Meta token [{brand.slug}]", "token is not valid")
        return

    if str(info.get("type", "")).upper() != "PAGE":
        report.fail(
            f"Meta token [{brand.slug}]",
            f"this is a {info.get('type')} token, not a PAGE token",
            fix="take the Page's access_token from GET /me/accounts",
        )
        return

    expires_at = info.get("expires_at", 0)
    if not expires_at:
        report.ok(f"Meta token [{brand.slug}]", "long-lived Page token, never expires")
        return

    left = dt.datetime.fromtimestamp(expires_at, dt.UTC) - dt.datetime.now(dt.UTC)
    hours = left.total_seconds() / 3600
    if hours <= 0:
        report.fail(f"Meta token [{brand.slug}]", "expired", fix="re-run the token exchange")
    elif hours < 48:
        report.fail(
            f"Meta token [{brand.slug}]",
            f"short-lived, {hours:.0f}h left — unusable for an unattended scheduler",
            fix="exchange it: spike/meta_exchange_token.py --brand <slug> --write-env",
        )
    elif left.days < TOKEN_WARN_DAYS:
        report.warn(f"Meta token [{brand.slug}]", f"expires in {left.days} day(s)")
    else:
        report.warn(f"Meta token [{brand.slug}]", f"expires in {left.days} days; prefer a never-expiring token")


def _check_instagram(report: DoctorReport, settings: Settings, brand, token: str, page_id: str) -> None:
    import httpx

    try:
        linked = (
            httpx.get(
                f"{GRAPH_API_BASE}/{page_id}",
                params={"fields": "instagram_business_account{id,username}", "access_token": token},
                timeout=20,
            )
            .json()
            .get("instagram_business_account")
        )
    except Exception as exc:
        report.fail(f"Instagram [{brand.slug}]", str(exc)[:160])
        return

    if not linked:
        report.fail(
            f"Instagram [{brand.slug}]",
            "no Instagram Business account linked to this Page",
            fix="link one, or remove IG from this brand's Enabled Platforms",
        )
        return

    if str(linked["id"]) != brand.instagram_user_id:
        report.fail(
            f"Instagram [{brand.slug}]",
            f"_Brands says {brand.instagram_user_id}, the Page is linked to {linked['id']}",
            fix="correct the Instagram User ID in _Brands",
        )
        return

    report.ok(f"Instagram [{brand.slug}]", f"@{linked.get('username')} ({linked['id']})")

    try:
        quota = httpx.get(
            f"{GRAPH_API_BASE}/{linked['id']}/content_publishing_limit",
            params={"fields": "config,quota_usage", "access_token": token},
            timeout=20,
        ).json()
        if data := quota.get("data"):
            used = data[0].get("quota_usage", 0)
            total = data[0].get("config", {}).get("quota_total", "?")
            level = report.warn if isinstance(total, int) and used >= total * 0.8 else report.ok
            level(f"Instagram quota [{brand.slug}]", f"{used} of {total} used in 24h")
    except Exception:
        report.warn(f"Instagram quota [{brand.slug}]", "could not read the publishing limit")


def _check_linkedin(report: DoctorReport, settings: Settings, brands: list) -> None:
    wants_li = [b for b in brands if Platform.LI in b.enabled_platforms]
    if not wants_li:
        return

    slugs = ", ".join(b.slug for b in wants_li)
    if not settings.has_linkedin:
        # Expected while access is pending — a warning, not a failure.
        report.warn(
            "LinkedIn",
            f"LINKEDIN_ACCESS_TOKEN unset; {slugs} cannot publish",
            fix="Phase 6 — awaiting Community Management API access",
        )
        return

    # The adapter has never run against the live API. Saying so here is the
    # difference between a surprise and an expected first-run adjustment.
    report.warn(
        "LinkedIn adapter",
        "written from the published docs and never run against the live API — "
        "expect to adjust it on the first real post",
    )

    expires = settings.linkedin_expires_at
    if expires is None:
        report.warn("LinkedIn token", "LINKEDIN_TOKEN_EXPIRES unset — expiry cannot be checked")
        return

    left = (expires - dt.datetime.now(dt.UTC)).days
    if left <= 0:
        report.fail("LinkedIn token", "expired", fix="postpilot auth linkedin")
    elif left <= 7:
        report.warn("LinkedIn token", f"expires in {left} day(s)", fix="postpilot auth linkedin")
    else:
        report.ok("LinkedIn token", f"valid for {left} more day(s)")


def _check_notifiers(report: DoctorReport, settings: Settings) -> None:
    if not settings.has_notifier:
        # Optional by design: a missing notifier degrades reporting, never
        # delivery. `summary` falls back to the _Log tab. One warning, not one
        # per notifier — an unconfigured Telegram next to a working email is
        # a choice, not a problem.
        report.warn(
            "Daily summary",
            "no email (SMTP_*/SUMMARY_TO) or Telegram configured — "
            "`summary` will write the digest to the _Log tab instead",
        )
        return
    _check_email(report, settings)
    _check_telegram(report, settings)


def _check_email(report: DoctorReport, settings: Settings) -> None:
    from postpilot.notify import EmailNotifier

    config = settings.smtp
    if config is None:
        return
    # Log in only. Sending a test email on every `doctor` run trains people to
    # ignore the sender, which is the one thing the digest cannot afford.
    ok, detail = EmailNotifier(config).check_login()
    if ok:
        report.ok("Email (SMTP)", f"{detail} → {', '.join(config.recipients)}")
    else:
        report.fail(
            "Email (SMTP)",
            f"{config.host}:{config.port} — {detail}",
            fix="Gmail needs 2-Step Verification on and an app password "
            "(myaccount.google.com/apppasswords) in SMTP_PASSWORD, not the account password",
        )


def _check_telegram(report: DoctorReport, settings: Settings) -> None:
    import httpx

    credentials = settings.telegram
    if credentials is None:
        return

    token, chat_id = credentials
    try:
        me = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if not me.get("ok"):
            report.fail("Telegram", str(me.get("description", ""))[:160])
            return
        report.ok("Telegram bot", f"@{me['result'].get('username')}")

        sent = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "PostPilot doctor: Telegram is working."},
            timeout=15,
        ).json()
        if sent.get("ok"):
            report.ok("Telegram delivery", f"test message delivered to {chat_id}")
        else:
            report.fail(
                "Telegram delivery",
                str(sent.get("description", ""))[:160],
                fix="add the bot to the chat; supergroup IDs start with -100",
            )
    except Exception as exc:
        report.fail("Telegram", str(exc)[:160])
