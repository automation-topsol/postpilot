"""Configuration: `.env` for secrets, `config.yaml` for tunables.

Secrets are flat, one environment variable per value, so any single token can
be rotated without touching the others. Nothing here is ever written to the
Sheet or a log line.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
ENV_PATH = REPO_ROOT / ".env"

# Meta tokens are per brand (a Page token really is page-specific). LinkedIn's
# single member token administers every Company Page the operator admins, so
# it is flat and unsuffixed — only the org URN is per brand. See CLAUDE.md §0.1.
LINKEDIN_TOKEN_VARS = ("LINKEDIN_ACCESS_TOKEN", "LINKEDIN_REFRESH_TOKEN", "LINKEDIN_TOKEN_EXPIRES")


class Tunables(BaseModel):
    """`config.yaml` — everything safe to commit and read at a glance."""

    timezone: str = "Asia/Karachi"
    lease_minutes: int = 20
    max_attempts: int = 3
    backoff_base_seconds: int = 300
    prepare_lookahead_hours: int = 24
    ig_container_timeout_seconds: int = 300
    reconcile_window_minutes: int = 30
    media_expiry_days: int = 60
    summary_hour_local: int = 9

    @classmethod
    def load(cls, path: Path | None = None) -> Tunables:
        path = path or CONFIG_PATH
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(**data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(), sort_keys=False, default_flow_style=False)


class MissingSetting(RuntimeError):
    """A required secret is absent. Always names the variable."""


def load_dotenv_once(path: Path | None = None) -> None:
    from dotenv import load_dotenv

    path = path or ENV_PATH
    if path.exists():
        load_dotenv(path, override=False)


def _env(name: str) -> str:
    """Empty and whitespace-only count as absent — `.env.example` ships blanks."""
    return os.environ.get(name, "").strip()


class Settings(BaseModel):
    """Resolved runtime configuration. Build with `Settings.load()`."""

    tunables: Tunables = Field(default_factory=Tunables)

    model_config = {"arbitrary_types_allowed": True}

    @classmethod
    def load(cls, *, dotenv: bool = True) -> Settings:
        if dotenv:
            load_dotenv_once()
        return cls(tunables=Tunables.load())

    # -- Google -------------------------------------------------------------
    @property
    def sheet_id(self) -> str:
        if value := _env("SHEET_ID"):
            return value
        raise MissingSetting("SHEET_ID is not set")

    def service_account_info(self) -> dict:
        """The JSON key, from the env var or a local file path."""
        if raw := _env("GOOGLE_SERVICE_ACCOUNT_JSON"):
            return json.loads(raw)
        if path := _env("GOOGLE_SERVICE_ACCOUNT_FILE"):
            return json.loads(Path(path).read_text(encoding="utf-8"))
        raise MissingSetting("GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_SERVICE_ACCOUNT_FILE) is not set")

    @cached_property
    def google_credentials(self):
        from google.oauth2.service_account import Credentials

        return Credentials.from_service_account_info(
            self.service_account_info(),
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly",
            ],
        )

    @property
    def service_account_email(self) -> str:
        return self.service_account_info().get("client_email", "")

    # -- R2 -----------------------------------------------------------------
    @property
    def r2(self) -> dict[str, str]:
        names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL")
        values = {n: _env(n) for n in names}
        if missing := [n for n, v in values.items() if not v]:
            raise MissingSetting(f"not set: {', '.join(missing)}")
        return values

    @property
    def r2_public_base(self) -> str:
        return self.r2["R2_PUBLIC_BASE_URL"].rstrip("/")

    # -- Meta ---------------------------------------------------------------
    @property
    def meta_app(self) -> tuple[str, str]:
        app_id, secret = _env("META_APP_ID"), _env("META_APP_SECRET")
        if not (app_id and secret):
            raise MissingSetting("META_APP_ID and META_APP_SECRET are required for `auth meta`")
        return app_id, secret

    def meta_page_token(self, brand_slug: str) -> str:
        var = "META_PAGE_TOKEN_" + brand_slug.upper().replace("-", "_")
        if value := _env(var):
            return value
        raise MissingSetting(f"{var} is not set")

    def has_meta_token(self, brand_slug: str) -> bool:
        return bool(_env("META_PAGE_TOKEN_" + brand_slug.upper().replace("-", "_")))

    # -- LinkedIn (one token for every page; Phase 6) ------------------------
    @property
    def linkedin_access_token(self) -> str:
        if value := _env("LINKEDIN_ACCESS_TOKEN"):
            return value
        raise MissingSetting("LINKEDIN_ACCESS_TOKEN is not set (LinkedIn access pending)")

    @property
    def has_linkedin(self) -> bool:
        return bool(_env("LINKEDIN_ACCESS_TOKEN"))

    @property
    def linkedin_expires_at(self) -> dt.datetime | None:
        raw = _env("LINKEDIN_TOKEN_EXPIRES")
        if not raw:
            return None
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None

    # -- Telegram (optional by design — see CLAUDE.md §0.5) -----------------
    @property
    def telegram(self) -> tuple[str, str] | None:
        """None when unconfigured. Callers fall back to `_Log`, never fail."""
        token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
        return (token, chat) if token and chat else None

    @property
    def has_telegram(self) -> bool:
        return self.telegram is not None


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    path = path or CONFIG_PATH
    if path.exists() and not overwrite:
        return path
    header = (
        "# PostPilot tunables. Secrets never live here — they are in .env.\n"
        "# Sheet dates are read in `timezone`; everything internal is UTC.\n"
    )
    path.write_text(header + Tunables().to_yaml(), encoding="utf-8")
    return path
