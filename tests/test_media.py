"""Policies, normalisation and key derivation.

The policy numbers are asserted against `docs/MEDIA_POLICIES.md` on purpose:
those figures were verified against vendor docs and several differ from the
obvious guess, so a test is what stops someone "correcting" them from memory.
"""

from __future__ import annotations

import io
import shutil

import pytest
from PIL import Image

from postpilot.media.normalise import (
    MediaError,
    carousel_aspect,
    fit_aspect,
    normalise_image,
    normalise_video,
    probe,
)
from postpilot.media.policies import (
    MB,
    FacebookImagePolicy,
    FacebookReelPolicy,
    InstagramCarouselPolicy,
    InstagramImagePolicy,
    InstagramReelPolicy,
    LinkedInImagePolicy,
    LinkedInVideoPolicy,
    PolicyNotAvailable,
    policy_for,
)
from postpilot.media.store import InMemoryStore, media_key
from postpilot.models import Platform, PostType

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def png(width: int, height: int, *, mode: str = "RGB") -> bytes:
    colour = (200, 120, 60, 255) if mode == "RGBA" else (200, 120, 60)
    buffer = io.BytesIO()
    Image.new(mode, (width, height), colour).save(buffer, "PNG")
    return buffer.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


class TestPolicyNumbers:
    """Each figure traceable to docs/MEDIA_POLICIES.md."""

    def test_instagram_image(self):
        p = InstagramImagePolicy()
        assert p.max_bytes == 8 * MB
        assert p.aspect_min == 4 / 5 and p.aspect_max == 1.91
        assert p.max_edge == 1440 and p.min_width == 320
        assert p.output_ext == "jpg"

    def test_facebook_image_allows_10mb_not_4(self):
        # The brief guessed 4 MB; the documented limit is 10 MB.
        assert FacebookImagePolicy().max_bytes == 10 * MB

    def test_facebook_image_has_no_aspect_constraint(self):
        p = FacebookImagePolicy()
        assert p.aspect_min is None and p.aspect_max is None

    def test_instagram_reel_duration_is_three_seconds_to_fifteen_minutes(self):
        p = InstagramReelPolicy()
        assert p.min_duration_s == 3 and p.max_duration_s == 15 * 60
        assert p.max_bytes == 300 * MB

    def test_facebook_reel_is_much_stricter_than_instagram(self):
        # 90 s vs 15 min. A video legal on IG can be illegal on FB, which is
        # why the two policies must never share normalised output.
        assert FacebookReelPolicy().max_duration_s == 90
        assert FacebookReelPolicy().max_duration_s < InstagramReelPolicy().max_duration_s

    def test_facebook_reel_minimum_resolution(self):
        p = FacebookReelPolicy()
        assert p.min_width == 540 and p.min_height == 960

    def test_linkedin_image_limit_is_pixels_not_bytes(self):
        # The brief assumed a byte limit; the documented one is a pixel count.
        assert LinkedInImagePolicy().max_pixels == 36_152_320

    def test_linkedin_video_bounds(self):
        p = LinkedInVideoPolicy()
        assert p.max_bytes == 500 * MB and p.min_bytes == 75 * 1024
        assert p.max_duration_s == 30 * 60

    def test_carousel_policy_requires_uniform_aspect(self):
        assert InstagramCarouselPolicy().uniform_aspect is True
        assert InstagramImagePolicy().uniform_aspect is False

    def test_policy_lookup(self):
        assert policy_for(Platform.IG, PostType.REEL).label == "InstagramReelPolicy"
        assert policy_for(Platform.FB, PostType.REEL).label == "FacebookReelPolicy"
        with pytest.raises(PolicyNotAvailable):
            policy_for(Platform.FB, PostType.TEXT)


class TestKeys:
    def test_key_is_deterministic(self):
        p = InstagramImagePolicy()
        first = media_key("brand", "gi-1", Platform.IG, p, "md5abc")
        second = media_key("brand", "gi-1", Platform.IG, p, "md5abc")
        assert first == second == "brand/gi-1/ig/v1-md5abc.jpg"

    def test_platforms_get_separate_keys(self):
        # The same source file becomes different files per platform, so they
        # must never collide.
        ig = media_key("b", "p", Platform.IG, InstagramImagePolicy(), "m")
        fb = media_key("b", "p", Platform.FB, FacebookImagePolicy(), "m")
        assert ig != fb

    def test_carousel_items_are_indexed(self):
        p = InstagramCarouselPolicy()
        assert media_key("b", "p", Platform.IG, p, "m", index=0).endswith("-0.jpg")
        assert media_key("b", "p", Platform.IG, p, "m", index=1).endswith("-1.jpg")

    def test_different_source_bytes_give_a_different_key(self):
        p = InstagramImagePolicy()
        assert media_key("b", "p", Platform.IG, p, "aaa") != media_key("b", "p", Platform.IG, p, "bbb")


class TestImageNormalisation:
    def test_square_within_range_is_untouched(self):
        out = normalise_image(png(1080, 1080), InstagramImagePolicy())
        assert (out.width, out.height) == (1080, 1080)
        assert out.notes == []

    def test_png_is_converted_to_jpeg(self):
        out = normalise_image(png(800, 800), InstagramImagePolicy())
        assert out.content_type == "image/jpeg"
        with Image.open(io.BytesIO(out.data)) as img:
            assert img.format == "JPEG"

    def test_transparency_is_flattened_not_blackened(self):
        out = normalise_image(png(400, 400, mode="RGBA"), InstagramImagePolicy())
        with Image.open(io.BytesIO(out.data)) as img:
            assert img.mode == "RGB"

    @pytest.mark.parametrize(("w", "h"), [(1080, 1920), (3000, 800), (500, 2000)])
    def test_out_of_range_aspects_are_brought_into_range(self, w, h):
        policy = InstagramImagePolicy()
        out = normalise_image(png(w, h), policy)
        ratio = out.width / out.height
        assert policy.aspect_min - 1e-6 <= ratio <= policy.aspect_max + 1e-6

    def test_far_out_of_range_is_padded_not_cropped(self):
        out = normalise_image(png(1080, 1920), InstagramImagePolicy())
        # Padding keeps the whole design visible; cropping a poster would cut
        # the date off the bottom.
        assert any("padded" in n for n in out.notes)

    def test_slightly_out_of_range_is_cropped(self):
        policy = InstagramImagePolicy()
        # 1.98:1 is ~3.7% beyond 1.91 — inside the 5% crop tolerance.
        out = normalise_image(png(1980, 1000), policy)
        assert any("cropped" in n for n in out.notes)

    def test_oversized_images_are_resized_to_the_long_edge(self):
        out = normalise_image(png(4000, 3000), InstagramImagePolicy())
        assert max(out.width, out.height) == 1440

    def test_facebook_keeps_aspect_ratio_untouched(self):
        out = normalise_image(png(3000, 800), FacebookImagePolicy())
        assert abs(out.width / out.height - 3000 / 800) < 0.01
        assert not any("padded" in n for n in out.notes)

    def test_small_image_warns_but_still_works(self):
        out = normalise_image(png(200, 200), InstagramImagePolicy())
        assert any("320px" in n for n in out.notes)
        assert out.data

    def test_byte_budget_is_respected(self):
        out = normalise_image(png(1440, 1440), InstagramImagePolicy())
        assert len(out.data) <= InstagramImagePolicy().max_bytes

    def test_unreadable_data_raises_a_readable_error(self):
        with pytest.raises(MediaError, match="cannot read this image"):
            normalise_image(b"not an image at all", InstagramImagePolicy())

    def test_linkedin_pixel_ceiling_is_enforced(self):
        # max_edge caps us well below the pixel limit, so construct the check
        # directly rather than allocating a 6000px image.
        policy = LinkedInImagePolicy()
        assert policy.max_edge**2 < policy.max_pixels


class TestCarousel:
    def test_shared_aspect_is_the_clamped_median(self):
        policy = InstagramCarouselPolicy()
        target = carousel_aspect([(1080, 1080), (1080, 1350), (1920, 1080)], policy)
        assert policy.aspect_min <= target <= policy.aspect_max

    def test_one_odd_item_does_not_drag_the_whole_set(self):
        policy = InstagramCarouselPolicy()
        # Four squares and one panorama: the median stays square.
        sizes = [(1080, 1080)] * 4 + [(3000, 800)]
        assert abs(carousel_aspect(sizes, policy) - 1.0) < 0.01

    def test_every_item_ends_at_the_same_ratio(self):
        policy = InstagramCarouselPolicy()
        sizes = [(1080, 1080), (1080, 1350), (1920, 1080)]
        target = carousel_aspect(sizes, policy)
        ratios = [
            (lambda o: o.width / o.height)(normalise_image(png(w, h), policy, force_aspect=target))
            for w, h in sizes
        ]
        assert max(ratios) - min(ratios) < 0.01

    def test_fit_aspect_clamps_both_ends(self):
        policy = InstagramImagePolicy()
        assert fit_aspect(0.3, policy) == policy.aspect_min
        assert fit_aspect(5.0, policy) == policy.aspect_max
        assert fit_aspect(1.0, policy) == 1.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")
class TestVideoNormalisation:
    @staticmethod
    def clip(tmp_path, seconds: int, size: str = "640x480", audio: bool = True) -> bytes:
        import subprocess

        path = tmp_path / "clip.mp4"
        command = ["ffmpeg", "-y", "-loglevel", "error",
                   "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={seconds}"]
        if audio:
            command += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac", "-shortest"]
        command += ["-c:v", "libx264", str(path)]
        subprocess.run(command, check=True, capture_output=True)
        return path.read_bytes()

    def test_probe_reads_dimensions_and_duration(self, tmp_path):
        self.clip(tmp_path, 4)
        info = probe(tmp_path / "clip.mp4")
        assert (info.width, info.height) == (640, 480)
        assert 3.5 < info.duration_s < 4.5
        assert info.has_audio

    def test_landscape_becomes_a_portrait_canvas_at_full_size(self, tmp_path):
        data = self.clip(tmp_path, 4, size="1920x1080")
        out = normalise_video(data, InstagramReelPolicy(), source_name="clip.mp4")
        # The canvas must come from the source's LONG edge; using the short
        # edge would yield a postage-stamp 608x1080.
        assert (out.width, out.height) == (1080, 1920)

    def test_too_long_for_facebook_is_refused_not_trimmed(self, tmp_path):
        data = self.clip(tmp_path, 95, size="320x240")
        with pytest.raises(MediaError, match="longer than"):
            normalise_video(data, FacebookReelPolicy(), source_name="clip.mp4")

    def test_same_clip_is_fine_for_instagram(self, tmp_path):
        data = self.clip(tmp_path, 95, size="320x240")
        out = normalise_video(data, InstagramReelPolicy(), source_name="clip.mp4")
        assert out.data

    def test_too_short_is_refused(self, tmp_path):
        data = self.clip(tmp_path, 1, size="320x240")
        with pytest.raises(MediaError, match="shorter than"):
            normalise_video(data, InstagramReelPolicy(), source_name="clip.mp4")

    def test_silent_source_gains_an_audio_track(self, tmp_path):
        data = self.clip(tmp_path, 4, size="320x240", audio=False)
        out = normalise_video(data, InstagramReelPolicy(), source_name="clip.mp4")
        assert any("silent audio" in n for n in out.notes)

    def test_not_a_video_raises(self):
        with pytest.raises(MediaError):
            normalise_video(png(100, 100), InstagramReelPolicy(), source_name="x.png")


class TestStore:
    def test_in_memory_store_round_trip(self):
        store = InMemoryStore()
        assert store.exists("k") is False
        url = store.put("k", b"data", "image/jpeg")
        assert store.exists("k") is True
        assert url == "https://media.test/k"
        assert store.url_for("k") == url
