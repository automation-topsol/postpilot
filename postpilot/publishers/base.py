"""The contract every platform adapter implements, and how results are classified.

Classification is the heart of the delivery guarantee, so it lives here rather
than inside each adapter:

| what happened                                   | result             | next run |
|-------------------------------------------------|--------------------|----------|
| definitive 2xx                                   | `SUCCESS`          | never again |
| definitive 4xx (validation, bad token)           | `PERMANENT_FAILURE`| never, without a human |
| 429/5xx/connection error **before** sending      | `RETRYABLE_FAILURE`| backs off and retries |
| timeout or reset **after** sending, or any error between the API's 2xx and our write | `UNKNOWN` | reconciles, never blindly retries |

An adapter that cannot tell which of the last two applies must return
`UNKNOWN`. Being wrong in that direction costs a human a glance; being wrong in
the other direction publishes twice, and v1 cannot delete a post.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from postpilot.models import Brand, Platform, Post


class PublishStatus(StrEnum):
    SUCCESS = "success"
    PERMANENT_FAILURE = "permanent_failure"
    RETRYABLE_FAILURE = "retryable_failure"
    UNKNOWN = "unknown"


@dataclass
class BrandCreds:
    """Credentials for one brand. Never logged, never written to the Sheet."""

    brand: Brand
    meta_page_token: str = ""
    linkedin_access_token: str = ""

    def token_for(self, platform: Platform) -> str:
        if platform is Platform.LI:
            return self.linkedin_access_token
        return self.meta_page_token


@dataclass
class PublishResult:
    status: PublishStatus
    remote_id: str = ""
    remote_url: str = ""
    error: str = ""
    # Instagram's container ID. Stored on UNKNOWN so reconciliation can ask
    # the API what became of it rather than guessing.
    container_id: str = ""

    @property
    def ok(self) -> bool:
        return self.status is PublishStatus.SUCCESS

    @classmethod
    def success(cls, remote_id: str, remote_url: str = "") -> PublishResult:
        return cls(PublishStatus.SUCCESS, remote_id=remote_id, remote_url=remote_url)

    @classmethod
    def permanent(cls, error: str) -> PublishResult:
        return cls(PublishStatus.PERMANENT_FAILURE, error=error)

    @classmethod
    def retryable(cls, error: str) -> PublishResult:
        return cls(PublishStatus.RETRYABLE_FAILURE, error=error)

    @classmethod
    def unknown(cls, error: str, container_id: str = "") -> PublishResult:
        return cls(PublishStatus.UNKNOWN, error=error, container_id=container_id)


@dataclass
class RemotePost:
    """A post already on the platform, used to resolve an `unknown`."""

    remote_id: str
    created_at: dt.datetime
    caption: str = ""
    url: str = ""


@dataclass
class PreparedPost:
    """What an adapter is handed: the row, plus public URLs for its media."""

    post: Post
    media_urls: list[str] = field(default_factory=list)
    caption: str = ""


@runtime_checkable
class Publisher(Protocol):
    platform: Platform

    def publish(self, prepared: PreparedPost, creds: BrandCreds) -> PublishResult: ...

    def find_recent(self, creds: BrandCreds, since: dt.datetime) -> list[RemotePost]:
        """Recent posts, newest first, for reconciliation.

        Raising means "I could not look" — which is NOT the same as "it is not
        there", and reconciliation treats the two very differently.
        """
        ...


def backoff_delay(attempts: int, base_seconds: int) -> dt.timedelta:
    """Exponential backoff: base, 2x, 4x… capped so a post never waits a day."""
    exponent = max(0, attempts - 1)
    seconds = min(base_seconds * (2**exponent), 6 * 60 * 60)
    return dt.timedelta(seconds=seconds)
