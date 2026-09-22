"""Structured logging with unconditional secret redaction.

Every HTTP call is logged to stdout, and runners keep stdout forever, so
redaction cannot be something a caller opts into. Anything that looks like a
token is masked on the way out regardless of who wrote the log line.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import sys
from typing import Any

LOGGER_NAME = "postpilot"

# Long opaque strings are almost always credentials. Meta tokens start EAA,
# and access_token/Authorization appear in URLs and headers we log verbatim.
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(access_token=)[^&\s\"']+"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"\bEAA[A-Za-z0-9]{20,}"),
    re.compile(r"(\"(?:access_token|refresh_token|client_secret|private_key)\"\s*:\s*\")[^\"]+"),
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]+?(-----END [A-Z ]*PRIVATE KEY-----)"),
)


def redact(text: str) -> str:
    """Mask anything credential-shaped. Cheap, and always applied."""
    if not text:
        return text
    out = text
    for pattern in _PATTERNS:
        if pattern.groups == 0:
            out = pattern.sub("<redacted>", out)
        elif pattern.groups == 1:
            out = pattern.sub(r"\1<redacted>", out)
        else:
            out = pattern.sub(r"\1<redacted>\2", out)
    return out


def redact_env_values(text: str) -> str:
    """Also mask the literal values of known-secret env vars, if present.

    Catches credentials whose shape the patterns above do not anticipate.
    """
    out = text
    for name, value in os.environ.items():
        if len(value) >= 12 and any(
            marker in name for marker in ("TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")
        ):
            out = out.replace(value, f"<{name}>")
    return out


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_env_values(redact(super().format(record)))


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def configure(*, verbose: bool = False, json_lines: bool = False) -> logging.Logger:
    """Configure stdout logging. Runners get stdout only; no file logs in CI."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    if json_lines:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    logger.addHandler(handler)
    return logger


class _JsonFormatter(RedactingFormatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.now(dt.UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if extra := getattr(record, "fields", None):
            payload.update(extra)
        return redact_env_values(redact(json.dumps(payload, default=str)))


def log_http(logger: logging.Logger, method: str, url: str, status: int | None, detail: str = "") -> None:
    """One line per API call. Redaction happens in the formatter regardless."""
    logger.info("HTTP %s %s -> %s %s", method.upper(), url, status if status is not None else "-", detail)
