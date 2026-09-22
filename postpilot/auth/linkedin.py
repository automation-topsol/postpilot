"""`postpilot auth linkedin` — acquire and refresh the single LinkedIn token.

**One token covers every Company Page the operator administers**, so this takes
no `--brand`. Only the org URN is per brand, and that lives in `_Brands`.
See CLAUDE.md §0.1.

Access tokens last ~60 days and refresh tokens ~1 year, so the refresh path is
the one that actually matters day to day: a scheduler that needs a human every
two months is a scheduler that stops working while nobody is looking.
`doctor` warns from 7 days out, and `summary` repeats the warning daily.
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from postpilot.logging import get_logger

log = get_logger(__name__)

AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"

# Must match the redirect URL registered on the LinkedIn app exactly.
CALLBACK_HOST, CALLBACK_PORT = "localhost", 8765
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}/callback"

# w_organization_social posts; r_organization_social is what lets
# reconciliation look, which is what keeps `unknown` resolvable.
SCOPES = ("w_organization_social", "r_organization_social")


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str = ""
    expires_at: dt.datetime | None = None
    refresh_expires_at: dt.datetime | None = None

    def env_lines(self) -> list[str]:
        """Exactly what to put in `.env` or `gh secret set`."""
        lines = [f"LINKEDIN_ACCESS_TOKEN={self.access_token}"]
        if self.refresh_token:
            lines.append(f"LINKEDIN_REFRESH_TOKEN={self.refresh_token}")
        if self.expires_at:
            lines.append(f"LINKEDIN_TOKEN_EXPIRES={self.expires_at.isoformat()}")
        return lines


class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # BaseHTTPRequestHandler's spelling, not ours
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code = (query.get("code") or [None])[0]
        _CallbackHandler.state = (query.get("state") or [None])[0]
        _CallbackHandler.error = (query.get("error_description") or query.get("error") or [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = "You can close this tab and go back to the terminal."
        if _CallbackHandler.error:
            message = f"LinkedIn returned an error: {_CallbackHandler.error}"
        self.wfile.write(f"<html><body><p>{message}</p></body></html>".encode())

    def log_message(self, *args) -> None:
        """Silence the default stderr access log."""


def authorize(client_id: str, client_secret: str, *, open_browser: bool = True) -> TokenBundle:
    """Run the authorization-code flow against a one-shot local server."""
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "state": state,
        "scope": " ".join(SCOPES),
    }
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    _CallbackHandler.code = _CallbackHandler.state = _CallbackHandler.error = None
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    print(f"\nOpen this URL and approve access:\n\n  {url}\n")
    if open_browser:
        webbrowser.open(url)

    thread.join(timeout=300)
    server.server_close()

    if _CallbackHandler.error:
        raise RuntimeError(f"LinkedIn refused: {_CallbackHandler.error}")
    if not _CallbackHandler.code:
        raise RuntimeError("no authorization code came back within 5 minutes")
    if _CallbackHandler.state != state:
        # A mismatched state means the response is not ours.
        raise RuntimeError("the state parameter did not match; aborting")

    return exchange_code(client_id, client_secret, _CallbackHandler.code)


def exchange_code(client_id: str, client_secret: str, code: str) -> TokenBundle:
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    return _bundle(response.json())


def refresh(client_id: str, client_secret: str, refresh_token: str) -> TokenBundle:
    """Swap a refresh token for a fresh access token.

    LinkedIn may or may not return a new refresh token; when it does not, the
    old one stays valid, so it is carried forward rather than blanked.
    """
    response = httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    response.raise_for_status()
    bundle = _bundle(response.json())
    if not bundle.refresh_token:
        bundle.refresh_token = refresh_token
    return bundle


def _bundle(payload: dict) -> TokenBundle:
    now = dt.datetime.now(dt.UTC)
    expires_in = int(payload.get("expires_in", 0) or 0)
    refresh_in = int(payload.get("refresh_token_expires_in", 0) or 0)
    return TokenBundle(
        access_token=str(payload.get("access_token", "")),
        refresh_token=str(payload.get("refresh_token", "") or ""),
        expires_at=now + dt.timedelta(seconds=expires_in) if expires_in else None,
        refresh_expires_at=now + dt.timedelta(seconds=refresh_in) if refresh_in else None,
    )


def needs_refresh(expires_at: dt.datetime | None, *, within_days: int = 7) -> bool:
    """True when the token expires inside the warning window, or already has."""
    if expires_at is None:
        return False
    return expires_at - dt.datetime.now(dt.UTC) <= dt.timedelta(days=within_days)
