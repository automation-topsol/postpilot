"""Instagram adapter: the container flow, the quota gate, and every place a
failure must become `unknown` rather than a retry."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from postpilot.apis import GRAPH_API_BASE
from postpilot.models import Brand, Platform, Post, PostType
from postpilot.publishers.base import BrandCreds, PreparedPost, PublishStatus
from postpilot.publishers.instagram import InstagramPublisher

IG_ID = "17841432916654917"
TOKEN = "EAAtest"
NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)

BRAND = Brand(
    name="Grand Invitation",
    slug="grandinvitation",
    enabled_platforms=[Platform.IG],
    instagram_user_id=IG_ID,
)
CREDS = BrandCreds(brand=BRAND, meta_page_token=TOKEN)


@pytest.fixture
def publisher() -> InstagramPublisher:
    # sleep is a no-op so polling costs nothing in tests.
    return InstagramPublisher(httpx.Client(), timeout_seconds=30, sleep=lambda _: None)


def prepared(post_type=PostType.IMAGE, urls=None, caption="Every love story…") -> PreparedPost:
    post = Post(
        post_id="gi-0001", brand_slug="grandinvitation", row_number=2,
        platforms=[Platform.IG], post_type=post_type, scheduled_at=NOW,
    )
    return PreparedPost(
        post=post,
        media_urls=urls if urls is not None else ["https://media.test/a.jpg"],
        caption=caption,
    )


def mock_quota(remaining: int = 99, total: int = 100) -> None:
    respx.get(url__startswith=f"{GRAPH_API_BASE}/{IG_ID}/content_publishing_limit").mock(
        return_value=httpx.Response(
            200, json={"data": [{"quota_usage": total - remaining, "config": {"quota_total": total}}]}
        )
    )


def mock_status(*statuses: str) -> None:
    respx.get(url__startswith=f"{GRAPH_API_BASE}/container-1").mock(
        side_effect=[httpx.Response(200, json={"status_code": s}) for s in statuses]
    )


class TestImage:
    @respx.mock
    def test_container_then_publish(self, publisher):
        mock_quota()
        create = respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "media-9"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/media-9").mock(
            return_value=httpx.Response(200, json={"permalink": "https://instagram.com/p/X/"})
        )

        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.SUCCESS
        assert result.remote_id == "media-9"
        assert result.remote_url == "https://instagram.com/p/X/"
        assert "image_url" in create.calls[0].request.content.decode()

    @respx.mock
    def test_polls_until_finished(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("IN_PROGRESS", "IN_PROGRESS", "FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "media-9"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/media-9").mock(
            return_value=httpx.Response(200, json={"permalink": "https://instagram.com/p/X/"})
        )
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.SUCCESS

    @respx.mock
    def test_a_missing_permalink_does_not_fail_a_successful_post(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "media-9"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/media-9").mock(return_value=httpx.Response(500))
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.SUCCESS and result.remote_url == ""


class TestQuota:
    @respx.mock
    def test_exhausted_quota_defers_without_burning_an_attempt(self, publisher):
        mock_quota(remaining=0)
        create = respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.RETRYABLE_FAILURE
        assert "24-hour publishing limit" in result.error
        # Nothing was attempted, so nothing was wasted.
        assert create.call_count == 0

    @respx.mock
    def test_an_unreadable_quota_does_not_block_publishing(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{IG_ID}/content_publishing_limit").mock(
            return_value=httpx.Response(500)
        )
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "media-9"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/media-9").mock(
            return_value=httpx.Response(200, json={"permalink": "p"})
        )
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.SUCCESS

    @respx.mock
    def test_remaining_quota_reads_the_numbers(self, publisher):
        mock_quota(remaining=37, total=100)
        assert publisher.remaining_quota(IG_ID, TOKEN) == 37


class TestUnknownWindows:
    """Every place where a retry could publish twice."""

    @respx.mock
    def test_media_publish_timeout_is_unknown_with_the_container_id(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        result = publisher.publish(prepared(), CREDS)
        # There is no idempotency key on media_publish.
        assert result.status is PublishStatus.UNKNOWN
        assert result.container_id == "container-1"

    @respx.mock
    def test_media_publish_5xx_is_unknown_not_retryable(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(return_value=httpx.Response(503))
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.UNKNOWN

    @respx.mock
    def test_media_publish_4xx_is_permanent(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(400, json={"error": {"message": "already published"}})
        )
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.PERMANENT_FAILURE

    @respx.mock
    def test_container_stuck_in_progress_times_out_as_unknown(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/container-1").mock(
            return_value=httpx.Response(200, json={"status_code": "IN_PROGRESS"})
        )
        slow = InstagramPublisher(httpx.Client(), timeout_seconds=0, sleep=lambda _: None)
        result = slow.publish(prepared(), CREDS)
        assert result.status is PublishStatus.UNKNOWN
        assert result.container_id == "container-1"

    @respx.mock
    def test_unreadable_container_status_is_unknown(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/container-1").mock(
            return_value=httpx.Response(500)
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.UNKNOWN


class TestContainerFailures:
    @respx.mock
    def test_container_error_is_permanent(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("ERROR")
        result = publisher.publish(prepared(), CREDS)
        # Instagram rejected the bytes; the same bytes will be rejected again.
        assert result.status is PublishStatus.PERMANENT_FAILURE

    @respx.mock
    def test_expired_container_is_permanent(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("EXPIRED")
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.PERMANENT_FAILURE

    @respx.mock
    def test_container_creation_4xx_is_a_clean_permanent_failure(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(400, json={"error": {"message": "bad image_url"}})
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "bad image_url" in result.error


class TestReel:
    @respx.mock
    def test_reel_sets_media_type_and_video_url(self, publisher):
        mock_quota()
        create = respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(200, json={"id": "container-1"})
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "media-r"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/media-r").mock(
            return_value=httpx.Response(200, json={"permalink": "p"})
        )
        publisher.publish(prepared(PostType.REEL, urls=["https://media.test/v.mp4"]), CREDS)
        body = create.calls[0].request.content.decode()
        assert "media_type=REELS" in body and "video_url" in body


class TestCarousel:
    @respx.mock
    def test_children_then_parent_then_publish(self, publisher):
        mock_quota()
        create = respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            side_effect=[
                httpx.Response(200, json={"id": "child-1"}),
                httpx.Response(200, json={"id": "child-2"}),
                httpx.Response(200, json={"id": "container-1"}),
            ]
        )
        mock_status("FINISHED")
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media_publish").mock(
            return_value=httpx.Response(200, json={"id": "media-c"})
        )
        respx.get(url__startswith=f"{GRAPH_API_BASE}/media-c").mock(
            return_value=httpx.Response(200, json={"permalink": "p"})
        )
        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        assert result.status is PublishStatus.SUCCESS
        assert create.call_count == 3
        assert "is_carousel_item" in create.calls[0].request.content.decode()
        parent = create.calls[2].request.content.decode()
        assert "media_type=CAROUSEL" in parent and "child-1" in parent and "child-2" in parent

    @respx.mock
    def test_a_failed_child_is_a_clean_failure(self, publisher):
        mock_quota()
        respx.post(f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            side_effect=[
                httpx.Response(200, json={"id": "child-1"}),
                httpx.Response(400, json={"error": {"message": "bad image"}}),
            ]
        )
        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        # Unlike Facebook, an abandoned IG container is invisible and expires
        # on its own, so this is safe to retry cleanly.
        assert result.status is PublishStatus.PERMANENT_FAILURE


class TestGuards:
    def test_text_is_rejected(self, publisher):
        result = publisher.publish(prepared(PostType.TEXT, urls=[]), CREDS)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "without media" in result.error

    def test_missing_ig_id_is_permanent(self, publisher):
        creds = BrandCreds(brand=Brand(name="X", slug="x", enabled_platforms=[Platform.IG]), meta_page_token="t")
        assert publisher.publish(prepared(), creds).status is PublishStatus.PERMANENT_FAILURE

    def test_missing_media_is_permanent(self, publisher):
        assert publisher.publish(prepared(urls=[]), CREDS).status is PublishStatus.PERMANENT_FAILURE


class TestFindRecent:
    @respx.mock
    def test_reads_media_and_filters_by_time(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "m1", "caption": "recent", "timestamp": "2026-10-07T13:50:00+0000",
                         "permalink": "https://instagram.com/p/A/"},
                        {"id": "m0", "caption": "old", "timestamp": "2026-01-01T00:00:00+0000",
                         "permalink": "https://instagram.com/p/B/"},
                    ]
                },
            )
        )
        recent = publisher.find_recent(CREDS, NOW - dt.timedelta(hours=1))
        assert [r.remote_id for r in recent] == ["m1"]

    @respx.mock
    def test_a_failed_lookup_raises(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{IG_ID}/media").mock(
            return_value=httpx.Response(403, json={"error": {"message": "nope"}})
        )
        with pytest.raises(RuntimeError, match="could not read the Instagram account"):
            publisher.find_recent(CREDS, NOW)

    @respx.mock
    def test_container_status_lookup(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/container-7").mock(
            return_value=httpx.Response(200, json={"status_code": "FINISHED"})
        )
        assert publisher.container_status("container-7", TOKEN) == "FINISHED"
