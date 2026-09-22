"""HTTP with result classification built in.

Every adapter goes through `call()`, because the *classification* of a failure
matters far more than the failure itself. The distinction that decides whether
a post can ever be retried:

- The connection never opened -> the request was **not sent** -> retryable.
- The connection opened and then timed out or was reset -> the request **may
  have been delivered** -> `unknown`, never a blind retry.

httpx exposes exactly this difference, and it is the reason adapters never call
httpx directly.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from postpilot.logging import get_logger, log_http
from postpilot.publishers.base import PublishResult

log = get_logger(__name__)

DEFAULT_TIMEOUT = 60.0
UPLOAD_TIMEOUT = 300.0

# Errors raised while establishing the connection: nothing was ever sent.
_BEFORE_SEND = (httpx.ConnectError, httpx.ConnectTimeout, httpx.TooManyRedirects, httpx.ProxyError)

# Errors raised after the request went out: the server may well have acted.
_AFTER_SEND = (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError, httpx.ReadError)


@dataclass
class Call:
    """Either a response, or a already-classified failure. Never both."""

    response: httpx.Response | None = None
    failure: PublishResult | None = None

    @property
    def ok(self) -> bool:
        return self.response is not None and self.failure is None

    def json(self) -> dict:
        return self.response.json() if self.response is not None else {}


def call(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs,
) -> Call:
    """Make one request and classify anything that goes wrong."""
    try:
        response = client.request(method, url, timeout=timeout, **kwargs)
    except _BEFORE_SEND as exc:
        log_http(log, method, url, None, f"connect failed: {exc}")
        return Call(failure=PublishResult.retryable(f"could not reach the API: {exc}"))
    except _AFTER_SEND as exc:
        # The request left the machine. We do not know what the server did.
        log_http(log, method, url, None, f"no answer after sending: {exc}")
        return Call(failure=PublishResult.unknown(f"no answer after sending the request: {exc}"))
    except httpx.HTTPError as exc:
        log_http(log, method, url, None, f"transport error: {exc}")
        return Call(failure=PublishResult.unknown(f"transport error: {exc}"))

    log_http(log, method, url, response.status_code)

    if response.is_success:
        return Call(response=response)

    return Call(failure=classify_status(response))


def classify_status(response: httpx.Response) -> PublishResult:
    """Turn a non-2xx into the right kind of failure."""
    message = error_message(response)
    status = response.status_code

    if status == 429:
        return PublishResult.retryable(f"rate limited (429): {message}")
    if status >= 500:
        return PublishResult.retryable(f"server error ({status}): {message}")
    # Every other 4xx is our fault and will not fix itself.
    return PublishResult.permanent(f"rejected ({status}): {message}")


def error_message(response: httpx.Response) -> str:
    """Pull the useful sentence out of a platform's error body.

    Meta and LinkedIn nest theirs differently, and the raw body is mostly
    noise — but the message is what reaches the teammate's Error column.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:200].strip() or response.reason_phrase

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            parts = [
                str(error.get("message", "")).strip(),
                f"(code {error['code']})" if error.get("code") is not None else "",
                f"subcode {error['error_subcode']}" if error.get("error_subcode") else "",
            ]
            if joined := " ".join(p for p in parts if p):
                return joined[:300]
        if isinstance(error, str) and error:
            return error[:300]
        for key in ("message", "error_description", "detail"):
            if value := body.get(key):
                return str(value)[:300]

    return response.text[:200].strip() or response.reason_phrase


def build_client(**kwargs) -> httpx.Client:
    """One client per run, so connections are reused across posts."""
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    return httpx.Client(**kwargs)
