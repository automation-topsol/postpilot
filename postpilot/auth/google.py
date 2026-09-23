"""`postpilot auth google` — sign in as a person, for uploading to Drive.

The service account can *read* the brand folders but cannot upload into them:
service accounts have no Drive storage of their own, so a create in a My Drive
folder fails with `storageQuotaExceeded`. Uploads therefore run as the
operator, through a one-time browser sign-in. Only the local `ui` importer
uses this; the scheduler never does, so nothing about publishing depends on it.

The token lives in `.postpilot/google-user-token.json` (gitignored), never in
`.env`, because it is personal and machine-local rather than a deploy secret.
"""

from __future__ import annotations

from pathlib import Path

from postpilot.config import REPO_ROOT, MissingSetting, _env
from postpilot.logging import get_logger

log = get_logger(__name__)

# Full Drive: `drive.file` only covers files this app created, and the brand
# folders were not, so an upload into them would 404 on the parent.
SCOPES = ["https://www.googleapis.com/auth/drive"]
TOKEN_PATH = REPO_ROOT / ".postpilot" / "google-user-token.json"


def client_config() -> dict:
    client_id, secret = _env("GOOGLE_OAUTH_CLIENT_ID"), _env("GOOGLE_OAUTH_CLIENT_SECRET")
    if not (client_id and secret):
        raise MissingSetting(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET are not set — create a "
            "'Desktop app' OAuth client in Google Cloud (see README, 'Local importer')"
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def load_credentials(path: Path = TOKEN_PATH):
    """Saved credentials, refreshed if needed; None when a sign-in is required."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except Exception as exc:
        log.warning("unreadable Google token at %s: %s", path, exc)
        return None
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:
            # An app in Google's "Testing" mode issues refresh tokens that die
            # after 7 days. That is a sign-in, not an error.
            log.info("Google token refresh failed, sign-in needed: %s", exc)
            return None
        save_credentials(creds, path)
        return creds
    return None


def save_credentials(creds, path: Path = TOKEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    path.chmod(0o600)


def sign_in(path: Path = TOKEN_PATH):
    """Open the browser, wait for consent, save and return the credentials."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(client_config(), SCOPES)
    creds = flow.run_local_server(
        port=0,
        open_browser=True,
        prompt="consent",
        access_type="offline",
        success_message="Signed in to PostPilot. You can close this tab.",
    )
    save_credentials(creds, path)
    return creds


def signed_in_email(creds) -> str:
    """Who we are uploading as — shown in the UI so nobody guesses."""
    from googleapiclient.discovery import build

    try:
        about = build("drive", "v3", credentials=creds, cache_discovery=False).about()
        return about.get(fields="user(emailAddress)").execute()["user"]["emailAddress"]
    except Exception:
        return ""
