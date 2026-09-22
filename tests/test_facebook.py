"""Facebook adapter against recorded HTTP shapes. No network, respx only."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import respx

from postpilot.apis import GRAPH_API_BASE, GRAPH_UPLOAD_BASE
from postpilot.models import Brand, Platform, Post, PostType
from postpilot.publishers.base import BrandCreds, PreparedPost, PublishStatus
from postpilot.publishers.facebook import FacebookPublisher

PAGE_ID = "1355072654348977"
TOKEN = "EAAtest"
NOW = dt.datetime(2026, 10, 7, 14, 0, tzinfo=dt.UTC)

BRAND = Brand(
    name="Grand Invitation",
    slug="grandinvitation",
    enabled_platforms=[Platform.FB],
    facebook_page_id=PAGE_ID,
)
CREDS = BrandCreds(brand=BRAND, meta_page_token=TOKEN)


def prepared(post_type=PostType.IMAGE, urls=None, caption="Every love story…") -> PreparedPost:
    post = Post(
        post_id="gi-0001",
        brand_slug="grandinvitation",
        row_number=2,
        platforms=[Platform.FB],
        post_type=post_type,
        scheduled_at=NOW,
    )
    return PreparedPost(post=post, media_urls=urls if urls is not None else ["https://media.test/a.jpg"], caption=caption)


@pytest.fixture
def publisher() -> FacebookPublisher:
    return FacebookPublisher(httpx.Client())


class TestImage:
    @respx.mock
    def test_records_post_id_not_photo_id(self, publisher):
        # Confirmed against the real API in Phase 0: these are different
        # numbers, and `id` alone gives a permalink that 404s.
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(
                200, json={"id": "122114927445466419", "post_id": f"{PAGE_ID}_122114927469466419"}
            )
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.SUCCESS
        assert result.remote_id == f"{PAGE_ID}_122114927469466419"
        assert result.remote_url.endswith(f"{PAGE_ID}_122114927469466419")

    @respx.mock
    def test_sends_the_media_url_and_caption(self, publisher):
        route = respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(200, json={"id": "1", "post_id": "p1"})
        )
        publisher.publish(prepared(urls=["https://media.test/x.jpg"], caption="hello"), CREDS)
        body = route.calls[0].request.content.decode()
        assert "https%3A%2F%2Fmedia.test%2Fx.jpg" in body
        assert "hello" in body

    @respx.mock
    def test_falls_back_to_id_when_post_id_is_absent(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(200, json={"id": "only-id"})
        )
        assert publisher.publish(prepared(), CREDS).remote_id == "only-id"


class TestClassification:
    @respx.mock
    def test_4xx_is_permanent(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(
                400,
                json={"error": {"message": "Invalid parameter", "code": 100, "error_subcode": 1234}},
            )
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "Invalid parameter" in result.error
        assert "code 100" in result.error

    @respx.mock
    def test_expired_token_is_permanent_and_readable(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(
                401, json={"error": {"message": "Session has expired", "code": 190}}
            )
        )
        result = publisher.publish(prepared(), CREDS)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "Session has expired" in result.error

    @respx.mock
    def test_429_is_retryable(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(429, json={"error": {"message": "rate limit"}})
        )
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.RETRYABLE_FAILURE

    @respx.mock
    def test_5xx_is_retryable(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(return_value=httpx.Response(503))
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.RETRYABLE_FAILURE

    @respx.mock
    def test_connect_error_is_retryable_because_nothing_was_sent(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(side_effect=httpx.ConnectError("refused"))
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.RETRYABLE_FAILURE

    @respx.mock
    def test_read_timeout_is_unknown_because_it_may_have_landed(self, publisher):
        # The request left the machine. Retrying could publish twice.
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(side_effect=httpx.ReadTimeout("timed out"))
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.UNKNOWN

    @respx.mock
    def test_connection_reset_after_sending_is_unknown(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            side_effect=httpx.RemoteProtocolError("peer closed connection")
        )
        assert publisher.publish(prepared(), CREDS).status is PublishStatus.UNKNOWN


class TestText:
    @respx.mock
    def test_text_goes_to_feed(self, publisher):
        route = respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/feed").mock(
            return_value=httpx.Response(200, json={"id": "p-text"})
        )
        result = publisher.publish(prepared(PostType.TEXT, urls=[], caption="words only"), CREDS)
        assert result.status is PublishStatus.SUCCESS and result.remote_id == "p-text"
        assert "words+only" in route.calls[0].request.content.decode()


class TestCarousel:
    @respx.mock
    def test_children_are_unpublished_then_attached(self, publisher):
        photos = respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            side_effect=[
                httpx.Response(200, json={"id": "c1"}),
                httpx.Response(200, json={"id": "c2"}),
            ]
        )
        feed = respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/feed").mock(
            return_value=httpx.Response(200, json={"id": "post-1"})
        )
        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        assert result.status is PublishStatus.SUCCESS and result.remote_id == "post-1"
        assert photos.call_count == 2
        for photo_call in photos.calls:
            assert "published=false" in photo_call.request.content.decode()
        attached = feed.calls[0].request.content.decode()
        assert "media_fbid" in attached and "c1" in attached and "c2" in attached

    @respx.mock
    def test_failure_on_the_first_child_is_a_clean_failure(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            return_value=httpx.Response(400, json={"error": {"message": "bad image"}})
        )
        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        # Nothing was created, so a retry is safe.
        assert result.status is PublishStatus.PERMANENT_FAILURE

    @respx.mock
    def test_failure_partway_through_children_is_unknown(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            side_effect=[
                httpx.Response(200, json={"id": "c1"}),
                httpx.Response(400, json={"error": {"message": "bad image"}}),
            ]
        )
        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        # A photo already exists on the Page; retrying would duplicate it.
        assert result.status is PublishStatus.UNKNOWN
        assert "half-created" in result.error

    @respx.mock
    def test_failure_creating_the_feed_post_is_unknown(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/photos").mock(
            side_effect=[httpx.Response(200, json={"id": "c1"}), httpx.Response(200, json={"id": "c2"})]
        )
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/feed").mock(return_value=httpx.Response(500))
        result = publisher.publish(
            prepared(PostType.CAROUSEL, urls=["https://m/1.jpg", "https://m/2.jpg"]), CREDS
        )
        assert result.status is PublishStatus.UNKNOWN
        assert "uploaded" in result.error


class TestReel:
    @respx.mock
    def test_three_phase_upload(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/video_reels").mock(
            side_effect=[
                httpx.Response(200, json={"video_id": "v-1", "upload_url": "https://rupload/x"}),
                httpx.Response(200, json={"success": True}),
            ]
        )
        respx.post(f"{GRAPH_UPLOAD_BASE}/video-upload/v-1").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        result = publisher.publish(
            prepared(PostType.REEL, urls=["https://media.test/v.mp4"]), CREDS
        )
        assert result.status is PublishStatus.SUCCESS and result.remote_id == "v-1"

    @respx.mock
    def test_failure_to_start_is_clean(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/video_reels").mock(
            return_value=httpx.Response(400, json={"error": {"message": "nope"}})
        )
        result = publisher.publish(prepared(PostType.REEL, urls=["https://m/v.mp4"]), CREDS)
        assert result.status is PublishStatus.PERMANENT_FAILURE

    @respx.mock
    def test_failure_after_the_slot_exists_is_unknown_and_keeps_the_id(self, publisher):
        respx.post(f"{GRAPH_API_BASE}/{PAGE_ID}/video_reels").mock(
            return_value=httpx.Response(200, json={"video_id": "v-9"})
        )
        respx.post(f"{GRAPH_UPLOAD_BASE}/video-upload/v-9").mock(return_value=httpx.Response(500))
        result = publisher.publish(prepared(PostType.REEL, urls=["https://m/v.mp4"]), CREDS)
        assert result.status is PublishStatus.UNKNOWN
        assert result.container_id == "v-9"


class TestGuards:
    def test_missing_page_id_is_permanent(self, publisher):
        creds = BrandCreds(brand=Brand(name="X", slug="x", enabled_platforms=[Platform.FB]), meta_page_token="t")
        result = publisher.publish(prepared(), creds)
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "Facebook Page ID" in result.error

    def test_missing_token_is_permanent(self, publisher):
        result = publisher.publish(prepared(), BrandCreds(brand=BRAND))
        assert result.status is PublishStatus.PERMANENT_FAILURE
        assert "META_PAGE_TOKEN_GRANDINVITATION" in result.error


class TestFindRecent:
    @respx.mock
    def test_uses_published_posts_not_feed(self, publisher):
        """/feed also returns visitor posts: it needs an extra App feature and
        those posts could false-match. Verified against the real API."""
        route = respx.get(url__startswith=f"{GRAPH_API_BASE}/{PAGE_ID}/published_posts").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        publisher.find_recent(CREDS, NOW)
        assert route.called

    @respx.mock
    def test_parses_graph_timestamps(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{PAGE_ID}/published_posts").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": f"{PAGE_ID}_1",
                            "message": "Every love story…",
                            "created_time": "2026-10-07T13:58:00+0000",
                            "permalink_url": "https://www.facebook.com/x",
                        }
                    ]
                },
            )
        )
        recent = publisher.find_recent(CREDS, NOW - dt.timedelta(hours=1))
        assert len(recent) == 1
        assert recent[0].created_at == dt.datetime(2026, 10, 7, 13, 58, tzinfo=dt.UTC)
        assert recent[0].caption == "Every love story…"

    @respx.mock
    def test_a_failed_lookup_raises_rather_than_returning_empty(self, publisher):
        # Empty would read as "the post is not there" and could double-publish.
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{PAGE_ID}/published_posts").mock(
            return_value=httpx.Response(500, json={"error": {"message": "server error"}})
        )
        with pytest.raises(RuntimeError, match="could not read the Page's posts"):
            publisher.find_recent(CREDS, NOW - dt.timedelta(hours=1))

    @respx.mock
    def test_posts_without_a_timestamp_are_skipped(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{PAGE_ID}/published_posts").mock(
            return_value=httpx.Response(200, json={"data": [{"id": "1", "message": "x"}]})
        )
        assert publisher.find_recent(CREDS, NOW) == []

    @respx.mock
    def test_story_is_used_when_there_is_no_message(self, publisher):
        respx.get(url__startswith=f"{GRAPH_API_BASE}/{PAGE_ID}/published_posts").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "1", "story": "added a photo",
                                "created_time": "2026-10-07T13:00:00+0000"}]},
            )
        )
        assert publisher.find_recent(CREDS, NOW)[0].caption == "added a photo"
