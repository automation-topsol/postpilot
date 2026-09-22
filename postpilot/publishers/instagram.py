"""Instagram Business publishing via the Content Publishing API.

Everything is a two-step container flow, which is what makes Instagram the
riskiest adapter:

    POST /{ig_user}/media          -> creation_id
    GET  /{creation_id}?fields=status_code   (poll until FINISHED)
    POST /{ig_user}/media_publish  -> media id

A carousel adds a layer: each item is its own container, then a parent
`CAROUSEL` container holds them.

The dangerous window is between `media_publish` leaving the machine and its
answer arriving. There is no idempotency key, so a blind retry publishes twice.
Every failure from that point on is `unknown`, carrying the **container ID** so
reconciliation can ask the API what became of it instead of guessing.

The 24-hour publishing quota is checked *before* attempting, because hitting it
would otherwise burn a retry attempt on something that was never going to work.
"""

from __future__ import annotations

import datetime as dt
import time

import httpx

from postpilot.apis import GRAPH_API_BASE
from postpilot.logging import get_logger
from postpilot.models import Platform, PostType
from postpilot.publishers.base import BrandCreds, PreparedPost, PublishResult, RemotePost
from postpilot.publishers.http import build_client, call, error_message

log = get_logger(__name__)

POLL_INTERVAL_SECONDS = 5
# Matches config.yaml's ig_container_timeout_seconds. A container still not
# FINISHED after this is `unknown`, not a failure: it may yet complete.
DEFAULT_TIMEOUT_SECONDS = 300
RECENT_LIMIT = 25

# Terminal container states, per the Content Publishing docs.
_DONE = {"FINISHED", "ERROR", "EXPIRED", "PUBLISHED"}


class InstagramPublisher:
    platform = Platform.IG

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        sleep=time.sleep,
    ) -> None:
        self._client = client or build_client()
        self._timeout = timeout_seconds
        # Injectable so tests do not actually wait five seconds a poll.
        self._sleep = sleep

    # -- publishing ---------------------------------------------------------
    def publish(self, prepared: PreparedPost, creds: BrandCreds) -> PublishResult:
        ig_id = creds.brand.instagram_user_id
        token = creds.meta_page_token
        post = prepared.post

        if not ig_id:
            return PublishResult.permanent("this brand has no Instagram User ID in _Brands")
        if not token:
            return PublishResult.permanent(f"{creds.brand.token_env_var} is not set")
        if post.post_type is PostType.TEXT:
            return PublishResult.permanent("Instagram cannot post without media")
        if not prepared.media_urls:
            return PublishResult.permanent("no media was prepared for Instagram")

        # Check the quota first: a rejection here costs an attempt for nothing.
        quota = self.remaining_quota(ig_id, token)
        if quota is not None and quota <= 0:
            return PublishResult.retryable(
                "Instagram's 24-hour publishing limit is used up; will try again later"
            )

        if post.post_type is PostType.CAROUSEL:
            container = self._create_carousel(ig_id, token, prepared)
        elif post.post_type is PostType.REEL:
            container = self._create_container(
                ig_id, token, {"video_url": prepared.media_urls[0], "media_type": "REELS",
                               "caption": prepared.caption}
            )
        else:
            container = self._create_container(
                ig_id, token, {"image_url": prepared.media_urls[0], "caption": prepared.caption}
            )

        if isinstance(container, PublishResult):
            return container

        ready = self._await_container(container, token)
        if ready is not None:
            return ready

        return self._publish_container(ig_id, token, container)

    # -- containers ---------------------------------------------------------
    def _create_container(self, ig_id: str, token: str, fields: dict[str, str]) -> str | PublishResult:
        result = call(
            self._client,
            "POST",
            f"{GRAPH_API_BASE}/{ig_id}/media",
            data={**fields, "access_token": token},
        )
        if not result.ok:
            return result.failure
        container_id = str(result.json().get("id", ""))
        if not container_id:
            return PublishResult.permanent(f"Instagram returned no container id: {result.json()}")
        return container_id

    def _create_carousel(self, ig_id: str, token: str, prepared: PreparedPost) -> str | PublishResult:
        children: list[str] = []
        for url in prepared.media_urls:
            child = self._create_container(ig_id, token, {"image_url": url, "is_carousel_item": "true"})
            if isinstance(child, PublishResult):
                # Containers are ephemeral and expire on their own (24h), and
                # nothing is visible to anyone until media_publish. So an
                # abandoned child is harmless and this stays a clean failure.
                return child
            children.append(child)

        return self._create_container(
            ig_id,
            token,
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": prepared.caption,
            },
        )

    def _await_container(self, container_id: str, token: str) -> PublishResult | None:
        """Poll until FINISHED. Returns None on success, else a failure."""
        deadline = time.monotonic() + self._timeout
        status = "IN_PROGRESS"

        while True:
            probe = call(
                self._client,
                "GET",
                f"{GRAPH_API_BASE}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
                timeout=30,
            )
            if not probe.ok:
                return PublishResult.unknown(
                    f"could not check container {container_id}: {probe.failure.error}",
                    container_id=container_id,
                )

            body = probe.json()
            status = str(body.get("status_code", "")).upper()
            if status in _DONE:
                break
            if time.monotonic() >= deadline:
                # It may still finish. Reconciliation asks the API later.
                return PublishResult.unknown(
                    f"container {container_id} was still {status or 'processing'} after "
                    f"{self._timeout}s",
                    container_id=container_id,
                )
            self._sleep(POLL_INTERVAL_SECONDS)

        if status == "FINISHED":
            return None
        if status == "EXPIRED":
            return PublishResult.permanent(f"container {container_id} expired before it was published")
        # ERROR: Instagram rejected the media. Retrying the same bytes will not help.
        return PublishResult.permanent(
            f"Instagram rejected the media ({status}): {body.get('status', '')}"
        )

    def _publish_container(self, ig_id: str, token: str, container_id: str) -> PublishResult:
        result = call(
            self._client,
            "POST",
            f"{GRAPH_API_BASE}/{ig_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
        )
        if not result.ok:
            failure = result.failure
            # There is no idempotency key on media_publish. Anything other than
            # a definitive 4xx must never be retried blindly.
            if failure.status.value == "permanent_failure":
                return failure
            return PublishResult.unknown(
                f"media_publish did not confirm for container {container_id}: {failure.error}",
                container_id=container_id,
            )

        media_id = str(result.json().get("id", ""))
        return PublishResult.success(media_id, self._permalink(media_id, token))

    def _permalink(self, media_id: str, token: str) -> str:
        """Best effort: a missing permalink must not fail a successful post."""
        if not media_id:
            return ""
        probe = call(
            self._client,
            "GET",
            f"{GRAPH_API_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": token},
            timeout=30,
        )
        return str(probe.json().get("permalink", "")) if probe.ok else ""

    # -- quota --------------------------------------------------------------
    def remaining_quota(self, ig_id: str, token: str) -> int | None:
        """Posts left in the rolling 24-hour window, or None if unreadable.

        None means "could not tell", and the caller proceeds — a quota check
        that cannot be made must not block publishing.
        """
        probe = call(
            self._client,
            "GET",
            f"{GRAPH_API_BASE}/{ig_id}/content_publishing_limit",
            params={"fields": "config,quota_usage", "access_token": token},
            timeout=30,
        )
        if not probe.ok:
            return None
        data = probe.json().get("data") or []
        if not data:
            return None
        used = int(data[0].get("quota_usage", 0) or 0)
        total = data[0].get("config", {}).get("quota_total")
        return None if total is None else int(total) - used

    # -- reconciliation -----------------------------------------------------
    def find_recent(self, creds: BrandCreds, since: dt.datetime) -> list[RemotePost]:
        """Recent media on the account. Raises if it cannot look."""
        ig_id = creds.brand.instagram_user_id
        response = self._client.get(
            f"{GRAPH_API_BASE}/{ig_id}/media",
            params={
                "fields": "id,caption,timestamp,permalink",
                "limit": RECENT_LIMIT,
                "access_token": creds.meta_page_token,
            },
            timeout=30,
        )
        if not response.is_success:
            raise RuntimeError(f"could not read the Instagram account: {error_message(response)}")

        out: list[RemotePost] = []
        for item in response.json().get("data", []):
            created = _parse_time(item.get("timestamp", ""))
            if created is None or created < since:
                continue
            out.append(
                RemotePost(
                    remote_id=str(item.get("id", "")),
                    created_at=created,
                    caption=item.get("caption") or "",
                    url=item.get("permalink") or "",
                )
            )
        return out

    def container_status(self, container_id: str, token: str) -> str:
        """What became of a container. Used to resolve an `unknown`."""
        probe = call(
            self._client,
            "GET",
            f"{GRAPH_API_BASE}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        if not probe.ok:
            raise RuntimeError(f"could not read container {container_id}")
        return str(probe.json().get("status_code", "")).upper()


def _parse_time(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed.astimezone(dt.UTC)
