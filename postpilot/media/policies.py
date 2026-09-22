"""One policy per (platform, media kind) target.

**Every number here is verified and traceable to `docs/MEDIA_POLICIES.md`.**
That file is updated first when a limit changes; this one follows. Do not
"correct" a value from memory — the vendors' real limits differ from the
obvious guesses in several places, which is why the doc records the source URL
and the verification date beside each figure.

Anything that is *our* choice rather than a platform rule is marked so, to stop
a future reader "fixing" a non-bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from postpilot.apis import POLICY_VERSION
from postpilot.models import Platform, PostType

MB = 1024 * 1024


class MediaKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class PolicyNotAvailable(RuntimeError):
    """No policy exists for this combination — e.g. a text post has no media."""


@dataclass(frozen=True)
class MediaPolicy:
    """Shared shape. Subclasses below are the real targets."""

    platform: Platform = Platform.FB
    kind: MediaKind = MediaKind.IMAGE
    label: str = "MediaPolicy"

    # Output container/format we always normalise *to*.
    output_ext: str = "jpg"
    output_mime: str = "image/jpeg"

    max_bytes: int = 8 * MB

    @property
    def version(self) -> str:
        """Part of every R2 key, so changing a rule invalidates old output."""
        return POLICY_VERSION


@dataclass(frozen=True)
class ImagePolicy(MediaPolicy):
    kind: MediaKind = MediaKind.IMAGE

    # None = unconstrained. Aspect is width / height.
    aspect_min: float | None = None
    aspect_max: float | None = None
    max_edge: int | None = None
    min_width: int | None = None
    max_pixels: int | None = None

    # Crop instead of padding when the overshoot is this small: padding a
    # barely out-of-range image wastes more of the frame than trimming it.
    crop_tolerance: float = 0.05

    # All items in one carousel must share a single aspect ratio.
    uniform_aspect: bool = False


@dataclass(frozen=True)
class VideoPolicy(MediaPolicy):
    kind: MediaKind = MediaKind.VIDEO
    output_ext: str = "mp4"
    output_mime: str = "video/mp4"

    min_duration_s: float = 3.0
    max_duration_s: float = 900.0
    min_bytes: int = 0
    target_aspect: float | None = None   # None = keep the source ratio
    aspect_min: float | None = None
    aspect_max: float | None = None
    max_edge: int | None = None
    min_width: int | None = None
    min_height: int | None = None
    target_fps: int = 30
    fps_min: int = 23
    fps_max: int = 60
    video_bitrate: str = "8M"
    audio_bitrate: str = "128k"
    audio_rate: int = 48000


# --------------------------------------------------------------------------
# Instagram — docs/MEDIA_POLICIES.md §2
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class InstagramImagePolicy(ImagePolicy):
    """JPEG only, <= 8 MB, aspect 4:5-1.91:1, width 320-1440, sRGB."""

    platform: Platform = Platform.IG
    label: str = "InstagramImagePolicy"
    max_bytes: int = 8 * MB
    aspect_min: float = 4 / 5       # 0.80
    aspect_max: float = 1.91
    max_edge: int = 1440
    min_width: int = 320


@dataclass(frozen=True)
class InstagramCarouselPolicy(InstagramImagePolicy):
    """As the image policy, but every item shares one aspect ratio."""

    label: str = "InstagramCarouselPolicy"
    uniform_aspect: bool = True


@dataclass(frozen=True)
class InstagramReelPolicy(VideoPolicy):
    """MP4/MOV H.264|HEVC + AAC, 23-60 fps, 3 s-15 min, <= 300 MB, width <= 1920.

    Instagram accepts 0.01:1-10:1, so 9:16 is *our* normalisation target, not a
    platform rule — it is simply what the surface favours. Same for 30 fps.
    """

    platform: Platform = Platform.IG
    label: str = "InstagramReelPolicy"
    max_bytes: int = 300 * MB
    min_duration_s: float = 3.0
    max_duration_s: float = 15 * 60
    aspect_min: float = 0.01
    aspect_max: float = 10.0
    target_aspect: float = 9 / 16
    max_edge: int = 1920
    video_bitrate: str = "8M"


# --------------------------------------------------------------------------
# Facebook — docs/MEDIA_POLICIES.md §3
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class FacebookImagePolicy(ImagePolicy):
    """Accepts jpeg/bmp/png/gif/tiff up to 10 MB, any aspect ratio.

    We convert everything to JPEG anyway: it sidesteps the documented "PNG over
    1 MB may appear pixelated" caveat, and keeps one code path. No aspect
    constraint, so Facebook images are never padded.
    """

    platform: Platform = Platform.FB
    label: str = "FacebookImagePolicy"
    max_bytes: int = 10 * MB
    max_edge: int = 2048  # our choice: keeps files small, well inside 10 MB


@dataclass(frozen=True)
class FacebookReelPolicy(VideoPolicy):
    """3-90 s, 9:16, >= 540x960, H.264|H.265 + AAC-LC, 24-60 fps.

    Genuinely stricter than Instagram: 90 seconds against 15 minutes, and a
    real 9:16 requirement rather than a recommendation. A video that is legal
    on Instagram can be illegal here, which is why the two never share output.
    """

    platform: Platform = Platform.FB
    label: str = "FacebookReelPolicy"
    max_bytes: int = 300 * MB  # not documented; ours, to bound upload time
    min_duration_s: float = 3.0
    max_duration_s: float = 90.0
    target_aspect: float = 9 / 16
    min_width: int = 540
    min_height: int = 960
    max_edge: int = 1920
    fps_min: int = 24


# --------------------------------------------------------------------------
# LinkedIn — docs/MEDIA_POLICIES.md §4. Recorded, not yet exercised (Phase 6).
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class LinkedInImagePolicy(ImagePolicy):
    """JPG/GIF/PNG, under 36,152,320 **pixels**.

    Note the limit is a pixel count, not a byte size — the brief assumed bytes.
    36,152,320 px is about 6013x6013, far above anything we produce.
    """

    platform: Platform = Platform.LI
    label: str = "LinkedInImagePolicy"
    max_bytes: int = 10 * MB  # ours: no documented byte limit exists
    max_pixels: int = 36_152_320
    max_edge: int = 2048


@dataclass(frozen=True)
class LinkedInVideoPolicy(VideoPolicy):
    """MP4, 3 s-30 min, 75 KB-500 MB."""

    platform: Platform = Platform.LI
    label: str = "LinkedInVideoPolicy"
    max_bytes: int = 500 * MB
    min_bytes: int = 75 * 1024
    min_duration_s: float = 3.0
    max_duration_s: float = 30 * 60
    max_edge: int = 1920


# --------------------------------------------------------------------------
_POLICIES: dict[tuple[Platform, PostType], MediaPolicy] = {
    (Platform.IG, PostType.IMAGE): InstagramImagePolicy(),
    (Platform.IG, PostType.CAROUSEL): InstagramCarouselPolicy(),
    (Platform.IG, PostType.REEL): InstagramReelPolicy(),
    (Platform.FB, PostType.IMAGE): FacebookImagePolicy(),
    (Platform.FB, PostType.CAROUSEL): FacebookImagePolicy(),
    (Platform.FB, PostType.REEL): FacebookReelPolicy(),
    (Platform.LI, PostType.IMAGE): LinkedInImagePolicy(),
    (Platform.LI, PostType.CAROUSEL): LinkedInImagePolicy(),
    (Platform.LI, PostType.REEL): LinkedInVideoPolicy(),
}


def policy_for(platform: Platform, post_type: PostType) -> MediaPolicy:
    """The policy for one target. `text` has no media, so it raises."""
    try:
        return _POLICIES[(platform, post_type)]
    except KeyError:
        raise PolicyNotAvailable(f"{post_type.value} has no media policy for {platform.label}") from None


def all_policies() -> list[MediaPolicy]:
    """Distinct policies, for `doctor` and for documentation checks."""
    return list(dict.fromkeys(_POLICIES.values()))
