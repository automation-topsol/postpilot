"""Facebook Page publishing via the Graph API.

Four shapes, three endpoints:

| type     | how |
|----------|-----|
| image    | `POST /{page}/photos` with `url` |
| carousel | each photo `published=false`, then `POST /{page}/feed` with `attached_media` |
| reel     | `POST /{page}/video_reels` start -> upload -> finish |
| text     | `POST /{page}/feed` with `message` |

Two details that are easy to get wrong and expensive to get wrong:

1. **`post_id`, not `id`.** `/photos` returns both; `id` is the photo object and
   `post_id` is the feed post. Recording `id` gives a permalink that 404s and
   breaks reconciliation matching. Confirmed with real data in Phase 0:
   `122114927445466419` vs `..._122114927469466419`.
2. **A carousel is partially published the moment its children exist.** The
   unpublished photos are already on the Page's object graph, so a failure
   between creating them and creating the feed post is `unknown`, not a clean
   retry — retrying would leave orphaned photos and could double-post.
"""

from __future__ import annotations

import datetime as dt

import httpx

from postpilot.apis import GRAPH_API_BASE, GRAPH_UPLOAD_BASE
from postpilot.logging import get_logger
from postpilot.models import Platform, PostType
from postpilot.publishers.base import BrandCreds, PreparedPost, PublishResult, RemotePost
from postpilot.publishers.http import UPLOAD_TIMEOUT, build_client, call, error_message

log = get_logger(__name__)

# How many recent posts reconciliation looks through. The brief says 10; 25
# costs the same single request and covers a busy brand.
RECENT_LIMIT = 25

# `/published_posts`, NOT `/feed`. Two reasons, both found by calling the real
# API: `/feed` additionally returns visitor posts, so it demands the
# "Page Public Content Access" feature and fails with (#10) on an ordinary
# Page token — and those visitor posts could false-match during
# reconciliation. `/published_posts` is exactly "things this Page published",
# which is exactly what we are looking for.
RECENT_EDGE = "published_posts"


class FacebookPublisher:
    platform = Platform.FB

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or build_client()

    # -- publishing ---------------------------------------------------------
    def publish(self, prepared: PreparedPost, creds: BrandCreds) -> PublishResult:
        page_id = creds.brand.facebook_page_id
        token = creds.meta_page_token
        post = prepared.post

        if not page_id:
            return PublishResult.permanent("this brand has no Facebook Page ID in _Brands")
        if not token:
            return PublishResult.permanent(f"{creds.brand.token_env_var} is not set")

        if post.post_type is PostType.TEXT:
            return self._publish_text(page_id, token, prepared.caption)
        if post.post_type is PostType.IMAGE:
            return self._publish_image(page_id, token, prepared.media_urls[0], prepared.caption)
        if post.post_type is PostType.CAROUSEL:
            return self._publish_carousel(page_id, token, prepared.media_urls, prepared.caption)
        if post.post_type is PostType.REEL:
            return self._publish_reel(page_id, token, prepared.media_urls[0], prepared.caption)
        return PublishResult.permanent(f"cannot publish {post.post_type} to Facebook")

    def _publish_text(self, page_id: str, token: str, caption: str) -> PublishResult:
        result = call(
            self._client,
            "POST",
            f"{GRAPH_API_BASE}/{page_id}/feed",
            data={"message": caption, "access_token": token},
        )
        if not result.ok:
            return result.failure
        body = result.json()
        post_id = str(body.get("id", ""))
        return PublishResult.success(post_id, permalink(post_id))

    def _publish_image(self, page_id: str, token: str, image_url: str, caption: str) -> PublishResult:
        result = call(
            self._client,
            "POST",
            f"{GRAPH_API_BASE}/{page_id}/photos",
            data={"url": image_url, "caption": caption, "access_token": token},
        )
        if not result.ok:
            return result.failure
        body = result.json()
        # post_id is the feed post; id is only the photo object.
        post_id = str(body.get("post_id") or body.get("id", ""))
        return PublishResult.success(post_id, permalink(post_id))

    def _publish_carousel(
        self, page_id: str, token: str, image_urls: list[str], caption: str
    ) -> PublishResult:
        children: list[str] = []
        for index, url in enumerate(image_urls):
            result = call(
                self._client,
                "POST",
                f"{GRAPH_API_BASE}/{page_id}/photos",
                data={"url": url, "published": "false", "access_token": token},
            )
            if not result.ok:
                failure = result.failure
                if index == 0:
                    # Nothing exists yet, so this is a clean failure.
                    return failure
                # Some children already exist on the Page. Retrying would
                # create duplicates, so hand it to a human.
                return PublishResult.unknown(
                    f"carousel half-created: {len(children)} of {len(image_urls)} photos uploaded "
                    f"before this failed ({failure.error}). The unpublished photos need clearing "
                    f"by hand before this can be retried."
                )
            children.append(str(result.json().get("id", "")))

        payload: dict[str, str] = {"message": caption, "access_token": token}
        for index, child in enumerate(children):
            payload[f"attached_media[{index}]"] = f'{{"media_fbid":"{child}"}}'

        result = call(self._client, "POST", f"{GRAPH_API_BASE}/{page_id}/feed", data=payload)
        if not result.ok:
            failure = result.failure
            return PublishResult.unknown(
                f"all {len(children)} carousel photos were uploaded but the post was not created "
                f"({failure.error}). The photos are unpublished on the Page and need clearing by hand."
            )
        post_id = str(result.json().get("id", ""))
        return PublishResult.success(post_id, permalink(post_id))

    def _publish_reel(self, page_id: str, token: str, video_url: str, caption: str) -> PublishResult:
        # Phase 1 of 3: reserve an upload slot.
        started = call(
            self._client,
            "POST",
            f"{GRAPH_API_BASE}/{page_id}/video_reels",
            data={"upload_phase": "start", "access_token": token},
        )
        if not started.ok:
            return started.failure

        body = started.json()
        video_id = str(body.get("video_id", ""))
        if not video_id:
            return PublishResult.permanent(f"Facebook did not return a video_id: {body}")

        # Phase 2: hand Facebook the URL and let it fetch. Anything that goes
        # wrong from here is `unknown` — the video slot exists on the Page.
        upload = call(
            self._client,
            "POST",
            f"{GRAPH_UPLOAD_BASE}/video-upload/{video_id}",
            headers={"Authorization": f"OAuth {token}", "file_url": video_url},
            timeout=UPLOAD_TIMEOUT,
        )
        if not upload.ok:
            return PublishResult.unknown(
                f"reel {video_id} was reserved but the upload did not complete "
                f"({upload.failure.error})",
                container_id=video_id,
            )

        # Phase 3: publish it.
        finished = call(
            self._client,
            "POST",
            f"{GRAPH_API_BASE}/{page_id}/video_reels",
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": caption,
                "access_token": token,
            },
            timeout=UPLOAD_TIMEOUT,
        )
        if not finished.ok:
            return PublishResult.unknown(
                f"reel {video_id} was uploaded but publishing it did not confirm "
                f"({finished.failure.error})",
                container_id=video_id,
            )
        return PublishResult.success(video_id, permalink(video_id))

    # -- reconciliation -----------------------------------------------------
    def find_recent(self, creds: BrandCreds, since: dt.datetime) -> list[RemotePost]:
        """Recent Page posts, newest first.

        Raises on failure. That is deliberate: the reconciler treats "I could
        not look" very differently from "it is not there", and returning an
        empty list would collapse the two and could cause a double publish.
        """
        page_id = creds.brand.facebook_page_id
        response = self._client.get(
            f"{GRAPH_API_BASE}/{page_id}/{RECENT_EDGE}",
            params={
                "fields": "id,message,story,created_time,permalink_url",
                "limit": RECENT_LIMIT,
                "since": int(since.timestamp()),
                "access_token": creds.meta_page_token,
            },
            timeout=30,
        )
        if not response.is_success:
            raise RuntimeError(f"could not read the Page's posts: {error_message(response)}")

        out: list[RemotePost] = []
        for item in response.json().get("data", []):
            created = _parse_time(item.get("created_time", ""))
            if created is None:
                continue
            out.append(
                RemotePost(
                    remote_id=str(item.get("id", "")),
                    created_at=created,
                    caption=item.get("message") or item.get("story") or "",
                    url=item.get("permalink_url") or permalink(str(item.get("id", ""))),
                )
            )
        return out


def permalink(post_id: str) -> str:
    return f"https://www.facebook.com/{post_id}" if post_id else ""


def _parse_time(value: str) -> dt.datetime | None:
    """Graph returns e.g. 2026-09-23T03:14:15+0000."""
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
