"""LinkedIn adapter and token refresh.

These fixtures come from the published API documentation, **not** from recorded
real traffic — Community Management API access was pending when they were
written. They pin the shapes we believe in, so the first real run has something
to disagree with. When it does, fix the code *and* the fixture together.
"""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from postpilot.apis import LINKEDIN_API_BASE, LINKEDIN_VERSION
from postpilot.auth.linkedin import TOKEN_URL, TokenBundle, needs_refresh, refresh
from postpilot.models import Brand, Platform, Post, PostType
from postpilot.publishers.base import BrandCreds, PreparedPost, PublishStatus
from postpilot.publishers.linkedin import LinkedInPublisher

ORG = "urn:li:organization:12345"
TOKEN = "li-token"
NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)

BRAND = Brand(
    name="Restockly POS", slug="restocklypos",
    enabled_platforms=[Platform.LI], linkedin_org_urn=ORG,
)
CREDS = BrandCreds(brand=BRAND, linkedin_access_token=TOKEN)


@pytest.fixture
def publisher() -> LinkedInPublisher:
    # fetch is injected: LinkedIn wants the bytes, unlike Meta which fetches
    # media itself, so the adapter pulls our own R2 object back down.
    return LinkedInPublisher(httpx.Client(), fetch=lambda url: b"fake-bytes")


def prepared(post_type=PostType.IMAGE, urls=None, caption="Inventory that keeps up.") -> PreparedPost:
    post = Post(
        post_id="rp-0001", brand_slug="restocklypos", row_number=2,
        platforms=[Platform.LI], post_type=post_type, scheduled_at=NOW,
    )
    return PreparedPost(
        post=post,
        media_urls=urls if urls is not None else ["https://media.test/a.jpg"],
        caption=caption,
    )


def mock_post_created(urn: str = "urn:li:share:7000") -> respx.Route:
    return respx.post(f"{LINKEDIN_API_BASE}/posts").mock(
        return_value=httpx.Response(201, headers={"x-restli-id": urn}, json={})
    )


def mock_image_upload(urn: str = "urn:li:image:C4E10AQ") -> tuple[respx.Route, respx.Route]:
    init = respx.post(url__startswith=f"{LINKEDIN_API_BASE}/images").mock(
        return_value=httpx.Response(
            200, json={"value": {"uploadUrl": "https://upload.linkedin.test/x", "image": urn}}
        )
    )
    put = respx.put("https://upload.linkedin.test/x").mock(return_value=httpx.Response(201))
    return init, put


class TestHeaders:
    @respx.mock
    def test_every_request_carries_the_version_headers(self, publisher):
        mock_image_upload()
        route = mock_post_created()
        publisher.publish(prepared(), CREDS)
        sent = route.calls[0].request.headers
        assert sent["linkedin-version"] == LINKEDIN_VERSION
        assert sent["x-restli-protocol-version"] == "2.0.0"
        assert sent["authorization"] == f"Bearer {TOKEN}"


class TestText:
    @respx.mock
    def test_text_post_has_no_content_block(self, publisher):
        route = mock_post_created()
        result = publisher.publish(prepared(PostType.TEXT, urls=[]), CREDS)
        assert result.status is PublishStatus.SUCCESS
        assert result.remote_id == "urn:li:share:7000"
        assert "content" not in route.calls[0].request.read().decode()

    @respx.mock
    def test_the_urn_comes_from_the_header(self, publisher):
        mock_post_created("urn:li:share:999")
        assert publisher.publish(prepared(PostType.TEXT, urls=[]), CREDS).remote_id == "urn:li:share:999"

    @respx.mock
    def test_permalink_is_built_from_the_urn(self, publisher):
        mock_post_created("urn:li:share:999")
        result = publisher.publish(prepared(PostType.TEXT, urls=[]), CREDS)
        assert result.remote_url == "https://www.linkedin.com/feed/update/urn:li:share:999/"


class TestImage:
    @respx.mock
    def test_initialize_upload_then_put_then_post(self, publisher):
        init, put = mock_image_upload()
        route = mock_post_created()
        result = publisher.publish(prepared(), CREDS)

        assert result.status is PublishStatus.SUCCESS
        assert init.called and put.called
        body = route.calls[0].request.read().decode()
        assert "urn:li:image:C4E10AQ" in body
        assert ORG in body

    @respx.mock
    def test_a_failed_byte_upload_is_a_clean_failure(self, publisher):
        respx.post(url__startswith=f"{LINKEDIN_API_BASE}/images").mock(
            return_value=httpx.Response(
                200, json={"value": {"uploadUrl": "https://upload.linkedin.test/x", "image": "urn:li:image:1"}}
            )
        )
        respx.put("https://upload.linkedin.test/x").mock(return_value=httpx.Response(400))
        result = publisher.publish(prepared(), CREDS)
        # The asset has no bytes and is invisible; a retry gets a fresh URN.
        assert result.status is PublishStatus.PERMANENT_FAILURE

    @respx.mock
    def test_a_missing_upload_url_is_permanent(self, publisher):
        respx.post(url__startswith=f"{LINKEDIN_API_BASE}/images").mock(
            return_value=httpx.Response(200, json={"value": {}})
        )
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.PERMANENT_FAILURE


class TestMultiImage:
    @respx.mock
    def test_a_carousel_becomes_a_multiimage_post(self, publisher):
        respx.post(url__startswith=f"{LINKEDIN_API_BASE}/images").mock(
            side_effect=[
                httpx.Response(200, json={"value": {"uploadUrl": "https://upload.linkedin.test/1",
                                                    "image": "urn:li:image:A"}}),
                httpx.Response(200, json={"value": {"uploadUrl": "https://upload.linkedin.test/2",
                                                    "image": "urn:li:image:B"}}),
            ]
        )
        respx.put(url__startswith="https://upload.linkedin.test/").mock(return_value=httpx.Response(201))
        route = mock_post_created()

        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        assert result.status is PublishStatus.SUCCESS
        body = route.calls[0].request.read().decode()
        # LinkedIn has no carousel type; several images is a MultiImage post.
        assert "multiImage" in body and "urn:li:image:A" in body and "urn:li:image:B" in body


class TestVideo:
    @respx.mock
    def test_multipart_upload_collects_every_etag(self, publisher):
        respx.post(url__startswith=f"{LINKEDIN_API_BASE}/videos?action=initializeUpload").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": {
                        "video": "urn:li:video:V1",
                        "uploadToken": "tok",
                        "uploadInstructions": [
                            {"uploadUrl": "https://upload.linkedin.test/p1", "firstByte": 0, "lastByte": 4},
                            {"uploadUrl": "https://upload.linkedin.test/p2", "firstByte": 5, "lastByte": 9},
                        ],
                    }
                },
            )
        )
        respx.put("https://upload.linkedin.test/p1").mock(
            return_value=httpx.Response(200, headers={"etag": '"etag-1"'})
        )
        respx.put("https://upload.linkedin.test/p2").mock(
            return_value=httpx.Response(200, headers={"etag": '"etag-2"'})
        )
        finalize = respx.post(url__startswith=f"{LINKEDIN_API_BASE}/videos?action=finalizeUpload").mock(
            return_value=httpx.Response(200, json={})
        )
        mock_post_created()

        pub = LinkedInPublisher(httpx.Client(), fetch=lambda url: b"0123456789")
        result = pub.publish(prepared(PostType.REEL, urls=["https://media.test/v.mp4"]), CREDS)

        assert result.status is PublishStatus.SUCCESS
        body = finalize.calls[0].request.read().decode()
        # finalizeUpload needs every part's ETag, in order.
        assert "etag-1" in body and "etag-2" in body

    @respx.mock
    def test_a_part_with_no_etag_is_unknown(self, publisher):
        respx.post(url__startswith=f"{LINKEDIN_API_BASE}/videos?action=initializeUpload").mock(
            return_value=httpx.Response(
                200,
                json={"value": {"video": "urn:li:video:V1", "uploadToken": "t",
                                "uploadInstructions": [
                                    {"uploadUrl": "https://upload.linkedin.test/p1",
                                     "firstByte": 0, "lastByte": 4}]}},
            )
        )
        respx.put("https://upload.linkedin.test/p1").mock(return_value=httpx.Response(200))
        pub = LinkedInPublisher(httpx.Client(), fetch=lambda url: b"01234")
        result = pub.publish(prepared(PostType.REEL, urls=["https://m/v.mp4"]), CREDS)
        assert result.status is PublishStatus.UNKNOWN


class TestPostClassification:
    @respx.mock
    def test_4xx_on_the_post_is_permanent(self, publisher):
        mock_image_upload()
        respx.post(f"{LINKEDIN_API_BASE}/posts").mock(
            return_value=httpx.Response(400, json={"message": "INVALID_URN_ID", "status": 400})
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "INVALID_URN_ID" in result.error

    @respx.mock
    def test_5xx_on_the_post_is_unknown_not_retryable(self, publisher):
        mock_image_upload()
        respx.post(f"{LINKEDIN_API_BASE}/posts").mock(return_value=httpx.Response(503))
        # The post may exist. Retrying would duplicate it.
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.UNKNOWN

    @respx.mock
    def test_timeout_on_the_post_is_unknown(self, publisher):
        mock_image_upload()
        respx.post(f"{LINKEDIN_API_BASE}/posts").mock(side_effect=httpx.ReadTimeout("slow"))
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.UNKNOWN


class TestGuards:
    def test_missing_org_urn_is_permanent(self, publisher):
        creds = BrandCreds(brand=Brand(name="X", slug="x", enabled_platforms=[Platform.LI]),
                           linkedin_access_token="t")
        result = publisher.publish(prepared(), creds)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "Org URN" in result.error

    def test_missing_token_is_permanent(self, publisher):
        result = publisher.publish(prepared(), BrandCreds(brand=BRAND))
        assert "LINKEDIN_ACCESS_TOKEN" in result.error


class TestFindRecent:
    @respx.mock
    def test_parses_millisecond_timestamps(self, publisher):
        created_ms = int(dt.datetime(2026, 10, 7, 13, 50, tzinfo=dt.UTC).timestamp() * 1000)
        respx.get(url__startswith=f"{LINKEDIN_API_BASE}/posts").mock(
            return_value=httpx.Response(
                200,
                json={"elements": [{"id": "urn:li:share:1", "commentary": "hello",
                                    "createdAt": created_ms}]},
            )
        )
        recent = publisher.find_recent(CREDS, NOW - dt.timedelta(hours=1))
        assert len(recent) == 1
        assert recent[0].created_at == dt.datetime(2026, 10, 7, 13, 50, tzinfo=dt.UTC)

    @respx.mock
    def test_a_failed_lookup_raises(self, publisher):
        respx.get(url__startswith=f"{LINKEDIN_API_BASE}/posts").mock(
            return_value=httpx.Response(403, json={"message": "forbidden"})
        )
        with pytest.raises(RuntimeError, match="could not read the organisation's posts"):
            publisher.find_recent(CREDS, NOW)


class TestTokenRefresh:
    @respx.mock
    def test_refresh_returns_a_new_access_token(self):
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(
                200,
                json={"access_token": "new-access", "expires_in": 5184000,
                      "refresh_token": "new-refresh", "refresh_token_expires_in": 31536000},
            )
        )
        bundle = refresh("client", "secret", "old-refresh")
        assert bundle.access_token == "new-access"
        assert bundle.refresh_token == "new-refresh"
        assert bundle.expires_at is not None

    @respx.mock
    def test_the_old_refresh_token_is_carried_forward_when_none_is_returned(self):
        # LinkedIn does not always issue a new one; blanking it would lock us out.
        respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "new-access", "expires_in": 5184000})
        )
        assert refresh("client", "secret", "old-refresh").refresh_token == "old-refresh"

    def test_env_lines_are_ready_to_paste(self):
        bundle = TokenBundle(
            access_token="a", refresh_token="r",
            expires_at=dt.datetime(2026, 12, 1, tzinfo=dt.UTC),
        )
        lines = bundle.env_lines()
        assert lines[0] == "LINKEDIN_ACCESS_TOKEN=a"
        assert "LINKEDIN_REFRESH_TOKEN=r" in lines
        assert any(line.startswith("LINKEDIN_TOKEN_EXPIRES=") for line in lines)

    def test_needs_refresh_window(self):
        soon = dt.datetime.now(dt.UTC) + dt.timedelta(days=3)
        later = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
        assert needs_refresh(soon) is True
        assert needs_refresh(later) is False
        assert needs_refresh(None) is False


class TestRegistryGating:
    def test_linkedin_is_absent_without_a_token(self, monkeypatch):
        from postpilot.publishers.registry import available_publishers

        monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
        assert Platform.LI not in available_publishers()

    def test_linkedin_appears_once_a_token_exists(self, monkeypatch):
        from postpilot.publishers.registry import available_publishers

        monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "something")
        assert Platform.LI in available_publishers()
