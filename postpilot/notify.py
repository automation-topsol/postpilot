"""Notifiers for the daily digest — email today, Telegram optional, more later.

Every notifier has the same tiny shape: a `name` and a `send(subject, text)`
that returns `(delivered, detail)` and **never raises**. That is the whole
interface. Adding WhatsApp later is one class here plus one line in
`configured_notifiers`; `summary` does not change.

A notifier that is not configured simply is not in the list. The caller decides
what "nobody delivered" means (it means: write to `_Log`).
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx

TELEGRAM_API = "https://api.telegram.org"
SMTP_TIMEOUT = 30


def _tls_context() -> ssl.SSLContext:
    """Verify against certifi's CA bundle, not the OS store.

    python.org and uv builds of Python on macOS ship without a usable system
    CA store, so the default context fails Gmail's certificate with
    CERTIFICATE_VERIFY_FAILED. certifi is already here (httpx depends on it),
    and it is what every HTTP call in this tool verifies against anyway.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Notifier(Protocol):
    name: str

    def send(self, subject: str, text: str) -> tuple[bool, str]: ...


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]


class EmailNotifier:
    """Plain-text email over SMTP. Gmail + an app password is the tested setup.

    Port 465 means implicit TLS; anything else means STARTTLS. Plain,
    unencrypted SMTP is not offered — the password would cross the wire.
    """

    name = "email"

    def __init__(self, config: SmtpConfig, smtp_factory=None) -> None:
        self.config = config
        self._factory = smtp_factory

    def _connect(self) -> smtplib.SMTP:
        cfg = self.config
        if self._factory is not None:
            return self._factory(cfg.host, cfg.port, timeout=SMTP_TIMEOUT)
        context = _tls_context()
        if cfg.port == 465:
            return smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT, context=context)
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT)
        server.starttls(context=context)
        return server

    def check_login(self) -> tuple[bool, str]:
        """Authenticate and disconnect without sending — used by `doctor`."""
        try:
            with self._connect() as server:
                server.login(self.config.user, self.config.password)
        except smtplib.SMTPAuthenticationError:
            return False, "login refused"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:160]
        return True, f"logged in as {self.config.user}"

    def send(self, subject: str, text: str) -> tuple[bool, str]:
        cfg = self.config
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = cfg.sender
        message["To"] = ", ".join(cfg.recipients)
        message.set_content(text)
        try:
            with self._connect() as server:
                server.login(cfg.user, cfg.password)
                refused = server.send_message(message)
        except smtplib.SMTPAuthenticationError:
            return False, "email login refused — Gmail needs an app password, not the account password"
        except Exception as exc:
            return False, f"email send failed: {type(exc).__name__}: {exc}"[:200]

        delivered = [r for r in cfg.recipients if r not in (refused or {})]
        if not delivered:
            return False, "email refused for every recipient"
        detail = f"emailed {', '.join(delivered)}"
        if refused:
            detail += f" (refused: {', '.join(refused)})"
        return True, detail


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client=None) -> None:
        self.token = token
        self.chat_id = chat_id
        self._client = client

    def send(self, subject: str, text: str) -> tuple[bool, str]:
        # Telegram has no subject line; the digest's first line already is one.
        try:
            response = (self._client or httpx).post(
                f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=20,
            )
            body = response.json()
        except Exception as exc:
            return False, f"Telegram send failed: {exc}"

        if body.get("ok"):
            return True, f"Telegram delivered to {self.chat_id}"
        return False, f"Telegram refused: {body.get('description', '')}"


def configured_notifiers(settings) -> list[Notifier]:
    """Every notifier whose settings are complete, in delivery order."""
    notifiers: list[Notifier] = []
    if (smtp := settings.smtp) is not None:
        notifiers.append(EmailNotifier(smtp))
    if (telegram := settings.telegram) is not None:
        notifiers.append(TelegramNotifier(*telegram))
    return notifiers
