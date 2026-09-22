"""LinkedIn Company Page publishing via the Posts API.

> **UNVERIFIED AGAINST THE LIVE API.** Community Management API access was
> still pending when this was written, so every shape here comes from the
> published documentation rather than from a real call. The adapter is only
> registered when `LINKEDIN_ACCESS_TOKEN` is set, so it cannot be reached by
> accident, and `doctor` says so out loud. Expect to adjust it during the first
> real run — and when you do, add the fixtures, the way Phases 4 and 5 did.

Shape of the thing:

    images:     POST /rest/images?action=initializeUpload  -> uploadUrl + image URN
                PUT the bytes to uploadUrl
    videos:     POST /rest/videos?action=initializeUpload  -> 4 MB part instructions
                PUT each part, collect the ETag of every one
                POST /rest/videos?action=finalizeUpload with those ETags
    the post:   POST /rest/posts  (201, URN in the `x-restli-id` response header)

**One token covers every Company Page the operator administers** — see
CLAUDE.md §0.1. Only the org URN is per brand.
"""

from __future__ import annotations

import datetime as dt

import httpx

from postpilot.apis import LINKEDIN_API_BASE, LINKEDIN_RESTLI_VERSION, LINKEDIN_VERSION
from postpilot.logging import get_logger
from postpilot.models import Platform, PostType
from postpilot.publishers.base import BrandCreds, PreparedPost, PublishResult, RemotePost
from postpilot.publishers.http import UPLOAD_TIMEOUT, build_client, call, error_message

log = get_logger(__name__)

RECENT_LIMIT = 25
# The docs specify 4 MB parts for multipart video upload (`split -b 4194303`).
VIDEO_PART_BYTES = 4 * 1024 * 1024


def headers(token: str, *, json_body: bool = True) -> dict[str, str]:
    out = {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": LINKEDIN_RESTLI_VERSION,
    }
    if json_body:
        out["Content-Type"] = "application/json"
    return out


class LinkedInPublisher:
    platform = Platform.LI

    def __init__(self, client: httpx.Client | None = None, *, fetch=None) -> None:
        self._client = client or build_client()
        # LinkedIn wants the bytes, not a URL — unlike Meta, which fetches for
        # us. So the adapter has to pull our own R2 object back down.
        self._fetch = fetch or self._default_fetch

    def _default_fetch(self, url: str) -> bytes:
        response = self._client.get(url, timeout=UPLOAD_TIMEOUT)
        response.raise_for_status()
        return response.content

    # -- publishing ---------------------------------------------------------
    def publish(self, prepared: PreparedPost, creds: BrandCreds) -> PublishResult:
        org = creds.brand.linkedin_org_urn
        token = creds.linkedin_access_token
        post = prepared.post

        if not org:
            return PublishResult.permanent("this brand has no LinkedIn Org URN in _Brands")
        if not token:
            return PublishResult.permanent("LINKEDIN_ACCESS_TOKEN is not set")

        if post.post_type is PostType.TEXT:
            return self._create_post(org, token, prepared.caption, content=None)

        try:
            if post.post_type is PostType.REEL:
                asset = self._upload_video(org, token, prepared.media_urls[0])
                if isinstance(asset, PublishResult):
                    return asset
                content = {"media": {"id": asset}}
            elif post.post_type is PostType.CAROUSEL:
                urns: list[str] = []
                for url in prepared.media_urls:
                    asset = self._upload_image(org, token, url)
                    if isinstance(asset, PublishResult):
                        return asset
                    urns.append(asset)
                # LinkedIn has no carousel; several images is a MultiImage post.
                content = {"multiImage": {"images": [{"id": u} for u in urns]}}
            else:
                asset = self._upload_image(org, token, prepared.media_urls[0])
                if isinstance(asset, PublishResult):
                    return asset
                content = {"media": {"id": asset}}
        except httpx.HTTPError as exc:
            return PublishResult.retryable(f"could not fetch our own media to upload: {exc}")

        return self._create_post(org, token, prepared.caption, content=content)

    # -- assets -------------------------------------------------------------
    def _upload_image(self, org: str, token: str, url: str) -> str | PublishResult:
        init = call(
            self._client,
            "POST",
            f"{LINKEDIN_API_BASE}/images?action=initializeUpload",
            headers=headers(token),
            json={"initializeUploadRequest": {"owner": org}},
        )
        if not init.ok:
            return init.failure

        value = init.json().get("value", {})
        upload_url, urn = value.get("uploadUrl", ""), value.get("image", "")
        if not (upload_url and urn):
            return PublishResult.permanent(f"LinkedIn did not return an upload URL: {init.json()}")

        data = self._fetch(url)
        put = call(
            self._client,
            "PUT",
            upload_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"},
            content=data,
            timeout=UPLOAD_TIMEOUT,
        )
        if not put.ok:
            # The asset exists but has no bytes. Nothing is visible to anyone,
            # so a retry is clean — a new upload gets a fresh URN.
            return put.failure
        return str(urn)

    def _upload_video(self, org: str, token: str, url: str) -> str | PublishResult:
        data = self._fetch(url)
        init = call(
            self._client,
            "POST",
            f"{LINKEDIN_API_BASE}/videos?action=initializeUpload",
            headers=headers(token),
            json={
                "initializeUploadRequest": {
                    "owner": org,
                    "fileSizeBytes": len(data),
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                }
            },
        )
        if not init.ok:
            return init.failure

        value = init.json().get("value", {})
        urn = value.get("video", "")
        instructions = value.get("uploadInstructions", []) or []
        upload_token = value.get("uploadToken", "")
        if not (urn and instructions):
            return PublishResult.permanent(f"LinkedIn did not return upload instructions: {init.json()}")

        etags: list[str] = []
        for part in instructions:
            first = int(part.get("firstByte", 0))
            last = int(part.get("lastByte", len(data) - 1))
            chunk = data[first : last + 1]
            put = call(
                self._client,
                "PUT",
                part["uploadUrl"],
                headers={"Content-Type": "application/octet-stream"},
                content=chunk,
                timeout=UPLOAD_TIMEOUT,
            )
            if not put.ok:
                return put.failure
            # finalizeUpload needs every part's ETag, in order.
            etag = put.response.headers.get("etag", "").strip('"')
            if not etag:
                return PublishResult.unknown(
                    f"a video part uploaded but returned no ETag, so {urn} cannot be finalised"
                )
            etags.append(etag)

        finalize = call(
            self._client,
            "POST",
            f"{LINKEDIN_API_BASE}/videos?action=finalizeUpload",
            headers=headers(token),
            json={
                "finalizeUploadRequest": {
                    "video": urn,
                    "uploadToken": upload_token,
                    "uploadedPartIds": etags,
                }
            },
        )
        if not finalize.ok:
            return finalize.failure
        return str(urn)

    # -- the post -----------------------------------------------------------
    def _create_post(
        self, org: str, token: str, caption: str, *, content: dict | None
    ) -> PublishResult:
        payload: dict = {
            "author": org,
            "commentary": caption,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if content:
            payload["content"] = content

        result = call(
            self._client,
            "POST",
            f"{LINKEDIN_API_BASE}/posts",
            headers=headers(token),
            json=payload,
        )
        if not result.ok:
            failure = result.failure
            # The post either exists or it does not, and only a definitive 4xx
            # tells us which. Anything else must not be retried blindly.
            if failure.status.value == "permanent_failure":
                return failure
            return PublishResult.unknown(f"the post request did not confirm: {failure.error}")

        # The URN comes back in a header, not the body.
        urn = result.response.headers.get("x-restli-id", "") or str(result.json().get("id", ""))
        return PublishResult.success(urn, post_url(urn))

    # -- reconciliation -----------------------------------------------------
    def find_recent(self, creds: BrandCreds, since: dt.datetime) -> list[RemotePost]:
        """Recent posts by this organisation. Needs `r_organization_social`."""
        response = self._client.get(
            f"{LINKEDIN_API_BASE}/posts",
            params={"q": "author", "author": creds.brand.linkedin_org_urn, "count": RECENT_LIMIT},
            headers=headers(creds.linkedin_access_token, json_body=False),
            timeout=30,
        )
        if not response.is_success:
            raise RuntimeError(f"could not read the organisation's posts: {error_message(response)}")

        out: list[RemotePost] = []
        for item in response.json().get("elements", []):
            created_ms = item.get("createdAt") or item.get("publishedAt")
            if not created_ms:
                continue
            created = dt.datetime.fromtimestamp(int(created_ms) / 1000, dt.UTC)
            if created < since:
                continue
            urn = str(item.get("id", ""))
            out.append(
                RemotePost(
                    remote_id=urn,
                    created_at=created,
                    caption=item.get("commentary") or "",
                    url=post_url(urn),
                )
            )
        return out


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn else ""
