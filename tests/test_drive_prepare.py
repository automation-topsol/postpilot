"""Drive resolution and the prepare pipeline.

The rule under test throughout: **never guess**. An ambiguous file name, a
missing file or a Google-native file is an error with a message the teammate
can act on, not a best-effort substitution.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from PIL import Image

from postpilot.drive import DriveClient, DriveFile, Resolution
from postpilot.media.policies import (
    FacebookImagePolicy,
    InstagramImagePolicy,
)
from postpilot.media.store import InMemoryStore, media_key
from postpilot.models import Brand, Platform, Post, PostType
from postpilot.prepare import prepare, prepare_post_platform


def png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buffer, "PNG")
    return buffer.getvalue()


class FakeDrive(DriveClient):
    """Stands in for `DriveClient`, keeping its real resolution logic.

    Subclassed rather than reimplemented on purpose: `resolve` is where the
    ambiguity and rejection rules live, and a hand-written fake would let those
    rules drift away from what the tests claim to check. Only the two methods
    that touch the network are replaced, and `__init__` is not called.
    """

    def __init__(self, files: list[DriveFile], blobs: dict[str, bytes] | None = None) -> None:
        self._files = files
        self._blobs = blobs or {}
        self._folders = {}
        self.downloads: list[str] = []

    def list_folder(self, folder_id: str, *, refresh: bool = False) -> list[DriveFile]:
        return self._files

    def download(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return self._blobs.get(file_id, png(1080, 1080))


def drive_file(name: str, file_id: str = "", md5: str = "md5", mime: str = "image/png") -> DriveFile:
    return DriveFile(id=file_id or f"id-{name}", name=name, mime_type=mime, size=1000, md5=md5)


@pytest.fixture
def brand() -> Brand:
    return Brand(
        name="Grand Invitation",
        slug="grandinvitation",
        enabled_platforms=[Platform.FB, Platform.IG],
        facebook_page_id="1",
        instagram_user_id="2",
        drive_folder_id="folder",
    )


def make_post(**kwargs) -> Post:
    defaults = dict(
        post_id="gi-0001",
        brand_slug="grandinvitation",
        row_number=2,
        platforms=[Platform.FB, Platform.IG],
        post_type=PostType.IMAGE,
        media=["a.png"],
        scheduled_at=dt.datetime(2026, 10, 7, 13, 30, tzinfo=dt.UTC),
    )
    defaults.update(kwargs)
    return Post(**defaults)


class TestResolution:
    def test_resolves_by_name(self, brand):
        drive = FakeDrive([drive_file("a.png")])
        result = drive.resolve("folder", ["a.png"])
        assert result.ok and [f.name for f in result.files] == ["a.png"]

    def test_order_is_preserved_because_it_is_carousel_order(self, brand):
        drive = FakeDrive([drive_file("a.png"), drive_file("b.png"), drive_file("c.png")])
        result = drive.resolve("folder", ["c.png", "a.png", "b.png"])
        assert [f.name for f in result.files] == ["c.png", "a.png", "b.png"]

    def test_name_matching_is_case_insensitive(self):
        drive = FakeDrive([drive_file("Invite.PNG")])
        assert drive.resolve("folder", ["invite.png"]).ok

    def test_duplicate_names_are_ambiguous_never_guessed(self):
        drive = FakeDrive([drive_file("a.png", "id1"), drive_file("a.png", "id2")])
        result = drive.resolve("folder", ["a.png"])
        assert not result.ok
        assert "ambiguous" in result.errors[0]

    def test_missing_file_is_named_in_the_error(self):
        drive = FakeDrive([drive_file("a.png")])
        result = drive.resolve("folder", ["missing.png"])
        assert "no file named 'missing.png'" in result.errors[0]

    def test_google_native_files_are_rejected(self):
        drive = FakeDrive([drive_file("deck", mime="application/vnd.google-apps.presentation")])
        result = drive.resolve("folder", ["deck"])
        assert "Google" in result.errors[0]

    def test_non_media_files_are_rejected(self):
        drive = FakeDrive([drive_file("notes.pdf", mime="application/pdf")])
        result = drive.resolve("folder", ["notes.pdf"])
        assert "not an image or a video" in result.errors[0]

    def test_file_without_checksum_is_rejected(self):
        # The R2 key is built from Drive's md5; without it the file would be
        # re-uploaded on every run.
        drive = FakeDrive([drive_file("a.png", md5="")])
        result = drive.resolve("folder", ["a.png"])
        assert "checksum" in result.errors[0]

    def test_resolution_by_file_id(self):
        item = drive_file("a.png", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345")
        drive = FakeDrive([item])
        assert drive.resolve("folder", ["1AbCdEfGhIjKlMnOpQrStUvWxYz012345"]).ok

    def test_id_shaped_entry_not_in_folder_says_so(self):
        drive = FakeDrive([drive_file("a.png")])
        result = drive.resolve("folder", ["1AbCdEfGhIjKlMnOpQrStUvWxYz012345"])
        assert "not in this brand's Drive folder" in result.errors[0]

    def test_fingerprint_uses_identity_and_bytes_not_the_name(self):
        renamed = Resolution(files=[drive_file("new-name.png", "id-1", "abc")])
        original = Resolution(files=[drive_file("old-name.png", "id-1", "abc")])
        # Renaming in Drive must not re-open a published post.
        assert renamed.fingerprint == original.fingerprint

    def test_fingerprint_changes_when_bytes_change(self):
        before = Resolution(files=[drive_file("a.png", "id-1", "aaa")])
        after = Resolution(files=[drive_file("a.png", "id-1", "bbb")])
        assert before.fingerprint != after.fingerprint


class TestPreparePlatform:
    def test_uploads_once_per_platform(self, brand):
        drive = FakeDrive([drive_file("a.png")])
        store = InMemoryStore()
        post = make_post()
        resolution = drive.resolve("folder", post.media)

        fb = prepare_post_platform(post, brand, Platform.FB, resolution, drive, store)
        ig = prepare_post_platform(post, brand, Platform.IG, resolution, drive, store)

        assert fb.uploaded == 1 and ig.uploaded == 1
        assert fb.keys != ig.keys  # different policies, different objects
        assert len(store.objects) == 2

    def test_second_run_reuses_and_downloads_nothing(self, brand):
        drive = FakeDrive([drive_file("a.png")])
        store = InMemoryStore()
        post = make_post()
        resolution = drive.resolve("folder", post.media)

        prepare_post_platform(post, brand, Platform.FB, resolution, drive, store)
        downloads_after_first = len(drive.downloads)

        again = prepare_post_platform(post, brand, Platform.FB, resolution, drive, store)
        assert again.reused == 1 and again.uploaded == 0
        # The reuse path must not download the source again — that is the
        # whole point of the deterministic key.
        assert len(drive.downloads) == downloads_after_first

    def test_key_matches_the_documented_shape(self, brand):
        drive = FakeDrive([drive_file("a.png", md5="deadbeef")])
        store = InMemoryStore()
        post = make_post()
        result = prepare_post_platform(post, brand, Platform.IG, drive.resolve("f", post.media), drive, store)
        assert result.keys == [
            media_key("grandinvitation", "gi-0001", Platform.IG, InstagramImagePolicy(), "deadbeef")
        ]

    def test_text_posts_need_nothing(self, brand):
        drive = FakeDrive([])
        store = InMemoryStore()
        post = make_post(post_type=PostType.TEXT, media=[])
        result = prepare_post_platform(post, brand, Platform.FB, Resolution(), drive, store)
        assert result.ok and result.urls == [] and store.objects == {}

    def test_resolution_errors_propagate_and_nothing_uploads(self, brand):
        drive = FakeDrive([drive_file("a.png")])
        store = InMemoryStore()
        post = make_post(media=["missing.png"])
        result = prepare_post_platform(post, brand, Platform.FB, drive.resolve("f", post.media), drive, store)
        assert not result.ok
        assert store.objects == {}

    def test_dry_run_reports_keys_without_uploading(self, brand):
        drive = FakeDrive([drive_file("a.png")])
        store = InMemoryStore()
        post = make_post()
        result = prepare_post_platform(
            post, brand, Platform.FB, drive.resolve("f", post.media), drive, store, dry_run=True
        )
        assert result.urls and store.objects == {}
        assert any("would upload" in n for n in result.notes)

    def test_carousel_items_share_one_aspect_ratio(self, brand):
        files = [drive_file("a.png", "id-a"), drive_file("b.png", "id-b"), drive_file("c.png", "id-c")]
        blobs = {"id-a": png(1080, 1080), "id-b": png(1080, 1350), "id-c": png(1920, 1080)}
        drive = FakeDrive(files, blobs)
        store = InMemoryStore()
        post = make_post(post_type=PostType.CAROUSEL, media=["a.png", "b.png", "c.png"])

        result = prepare_post_platform(
            post, brand, Platform.IG, drive.resolve("f", post.media), drive, store
        )
        assert result.uploaded == 3
        ratios = []
        for key in result.keys:
            data, _ = store.objects[key]
            with Image.open(io.BytesIO(data)) as img:
                ratios.append(img.width / img.height)
        assert max(ratios) - min(ratios) < 0.01

    def test_carousel_keys_are_indexed_so_items_do_not_collide(self, brand):
        files = [drive_file("a.png", "id-a", md5="same"), drive_file("b.png", "id-b", md5="same")]
        drive = FakeDrive(files, {"id-a": png(800, 800), "id-b": png(800, 800)})
        store = InMemoryStore()
        post = make_post(post_type=PostType.CAROUSEL, media=["a.png", "b.png"])
        result = prepare_post_platform(
            post, brand, Platform.IG, drive.resolve("f", post.media), drive, store
        )
        # Identical md5s would otherwise map to one key and lose an item.
        assert len(set(result.keys)) == 2

    def test_storage_failure_is_reported_not_raised(self, brand):
        class BrokenStore(InMemoryStore):
            def exists(self, key: str) -> bool:
                raise RuntimeError("AccessDenied")

        drive = FakeDrive([drive_file("a.png")])
        post = make_post()
        result = prepare_post_platform(
            post, brand, Platform.FB, drive.resolve("f", post.media), drive, BrokenStore()
        )
        assert not result.ok and "cannot reach media storage" in result.errors[0]


class TestPrepareSelection:
    def _setup(self, brand, scheduled_at):
        drive = FakeDrive([drive_file("a.png")])
        store = InMemoryStore()
        post = make_post(scheduled_at=scheduled_at)
        return drive, store, [(brand, post)]

    def test_due_soon_is_prepared(self, brand):
        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
        drive, store, pairs = self._setup(brand, now + dt.timedelta(hours=2))
        result = prepare(pairs, drive, store, now=now, lookahead_hours=24)
        assert len(result.prepared) == 2  # FB and IG

    def test_far_future_is_left_alone(self, brand):
        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
        drive, store, pairs = self._setup(brand, now + dt.timedelta(days=30))
        result = prepare(pairs, drive, store, now=now, lookahead_hours=24)
        assert result.prepared == []

    def test_drafts_are_skipped_with_a_reason(self, brand):
        drive, store, pairs = self._setup(brand, None)
        result = prepare(pairs, drive, store, now=dt.datetime(2026, 10, 7, tzinfo=dt.UTC))
        assert result.prepared == [] and result.skipped

    def test_explicit_post_id_ignores_the_schedule(self, brand):
        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
        drive, store, pairs = self._setup(brand, now + dt.timedelta(days=90))
        result = prepare(pairs, drive, store, now=now, only_post="gi-0001")
        assert len(result.prepared) == 2

    def test_invalid_platforms_are_not_prepared(self, brand):
        from postpilot.models import ValidationIssue

        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
        drive, store, pairs = self._setup(brand, now)
        pairs[0][1].issues.append(ValidationIssue(platform=Platform.IG, message="nope"))
        result = prepare(pairs, drive, store, now=now)
        # Preparing media for a platform that cannot publish is wasted work
        # and a misleading error; sync already recorded why.
        assert [p.platform for p in result.prepared] == [Platform.FB]

    def test_facebook_and_instagram_get_different_bytes_for_a_tall_image(self, brand):
        now = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
        drive = FakeDrive([drive_file("tall.png", "id-tall")], {"id-tall": png(1080, 1920)})
        store = InMemoryStore()
        post = make_post(media=["tall.png"], scheduled_at=now)
        prepare([(brand, post)], drive, store, now=now)

        fb_key = media_key("grandinvitation", "gi-0001", Platform.FB, FacebookImagePolicy(), "md5")
        ig_key = media_key("grandinvitation", "gi-0001", Platform.IG, InstagramImagePolicy(), "md5")
        # Instagram pads 9:16 into 4:5; Facebook leaves it alone.
        assert store.objects[fb_key][0] != store.objects[ig_key][0]
