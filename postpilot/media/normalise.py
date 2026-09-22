"""Make a source file satisfy one platform's policy.

Two rules run through everything here:

1. **Pad, don't crop** (beyond a small tolerance). Cropping silently removes
   part of a design someone made deliberately; padding onto a blurred copy of
   the image keeps the whole subject visible. A wedding invitation with its
   date cropped off is worse than one with soft bars.
2. **Refuse rather than mangle.** Duration limits are not something to fix by
   trimming — a 4-minute video is not a 90-second video with the end removed.
   Those raise, and the message reaches the teammate's `Error` column.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from postpilot.logging import get_logger
from postpilot.media.policies import ImagePolicy, MediaKind, MediaPolicy, VideoPolicy

log = get_logger(__name__)

# Descending JPEG quality ladder, tried until the file fits the byte budget.
_QUALITY_LADDER = (92, 88, 84, 80, 75, 70, 65, 60)

# Blur radius for padded backgrounds, relative to the canvas long edge. Enough
# that the backdrop reads as texture rather than a second copy of the picture.
_BLUR_FRACTION = 0.03

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class MediaError(RuntimeError):
    """Normalisation cannot satisfy the policy. The message reaches the Sheet."""


@dataclass
class NormalisedMedia:
    data: bytes
    content_type: str
    width: int = 0
    height: int = 0
    duration_s: float = 0.0
    notes: list[str] | None = None

    @property
    def note_text(self) -> str:
        return "; ".join(self.notes or [])


def tools_available() -> dict[str, str | None]:
    return {FFMPEG: shutil.which(FFMPEG), FFPROBE: shutil.which(FFPROBE)}


def normalise(data: bytes, policy: MediaPolicy, *, source_name: str = "") -> NormalisedMedia:
    if policy.kind is MediaKind.IMAGE:
        return normalise_image(data, policy)  # type: ignore[arg-type]
    return normalise_video(data, policy, source_name=source_name)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------
def _flatten(img):
    """Apply EXIF rotation, then composite onto white.

    Phone cameras record orientation in EXIF rather than rotating the pixels,
    so a portrait photo arrives as landscape with a "rotate 90" tag. Pillow
    does not apply that automatically and JPEG re-encoding drops the tag, so
    without this the picture publishes sideways — and the aspect-ratio logic
    would pad the wrong axis on the way.
    """
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img) or img

    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        canvas = Image.new("RGB", img.size, (255, 255, 255))
        canvas.paste(img, mask=img.split()[-1])
        return canvas
    return img.convert("RGB")


def _encode(img, max_bytes: int) -> bytes:
    for quality in _QUALITY_LADDER:
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        if buffer.tell() <= max_bytes:
            return buffer.getvalue()
    raise MediaError(
        f"cannot compress below {max_bytes // 1024 // 1024} MB even at quality "
        f"{_QUALITY_LADDER[-1]} — use a smaller image"
    )


def fit_aspect(aspect: float, policy: ImagePolicy) -> float:
    """Clamp an aspect ratio into the policy's allowed range."""
    low = policy.aspect_min if policy.aspect_min is not None else aspect
    high = policy.aspect_max if policy.aspect_max is not None else aspect
    return min(max(aspect, low), high)


def normalise_image(
    data: bytes,
    policy: ImagePolicy,
    *,
    force_aspect: float | None = None,
) -> NormalisedMedia:
    """Convert to JPEG and make it satisfy `policy`.

    `force_aspect` makes every item of a carousel share one ratio — Instagram
    requires that, and it must be decided once for the whole set rather than
    per item.
    """
    from PIL import Image, ImageFilter

    try:
        img = _flatten(Image.open(io.BytesIO(data)))
    except Exception as exc:
        raise MediaError(f"cannot read this image ({exc})") from exc

    notes: list[str] = []
    width, height = img.size
    if not width or not height:
        raise MediaError("image has no dimensions")

    if policy.min_width and width < policy.min_width:
        notes.append(f"only {width}px wide; {policy.platform.label} prefers at least {policy.min_width}px")

    aspect = width / height
    target = force_aspect if force_aspect is not None else fit_aspect(aspect, policy)

    if abs(target - aspect) > 1e-6:
        overshoot = abs(target - aspect) / aspect
        if overshoot <= policy.crop_tolerance:
            img = _crop_to(img, target)
            notes.append(f"cropped slightly to {target:.2f}:1 (within {policy.crop_tolerance:.0%})")
        else:
            img = _pad_to(img, target, ImageFilter, Image)
            notes.append(f"padded with a blurred background to {target:.2f}:1")

    if policy.max_edge and max(img.size) > policy.max_edge:
        before = img.size
        img.thumbnail((policy.max_edge, policy.max_edge), Image.LANCZOS)
        notes.append(f"resized {before[0]}x{before[1]} to {img.size[0]}x{img.size[1]}")

    if policy.max_pixels and (img.size[0] * img.size[1]) > policy.max_pixels:
        raise MediaError(
            f"{img.size[0]}x{img.size[1]} exceeds {policy.platform.label}'s "
            f"{policy.max_pixels:,} pixel limit"
        )

    encoded = _encode(img, policy.max_bytes)
    return NormalisedMedia(
        data=encoded,
        content_type=policy.output_mime,
        width=img.size[0],
        height=img.size[1],
        notes=notes,
    )


def _crop_to(img, target: float):
    width, height = img.size
    if width / height > target:
        new_width = round(height * target)
        left = (width - new_width) // 2
        return img.crop((left, 0, left + new_width, height))
    new_height = round(width / target)
    top = (height - new_height) // 2
    return img.crop((0, top, width, top + new_height))


def _pad_to(img, target: float, ImageFilter, Image):
    """Centre the image on a blurred, enlarged copy of itself."""
    width, height = img.size
    if width / height > target:
        canvas = (width, round(width / target))
    else:
        canvas = (round(height * target), height)

    radius = max(8, int(max(canvas) * _BLUR_FRACTION))
    background = img.resize(canvas, Image.LANCZOS).filter(ImageFilter.GaussianBlur(radius))
    background.paste(img, ((canvas[0] - width) // 2, (canvas[1] - height) // 2))
    return background


def carousel_aspect(sizes: list[tuple[int, int]], policy: ImagePolicy) -> float:
    """One ratio for a whole carousel: the median, clamped into range.

    The median rather than the first item, so a single odd image cannot drag
    the whole set into heavy padding.
    """
    if not sizes:
        return 1.0
    ratios = sorted(w / h for w, h in sizes if h)
    median = ratios[len(ratios) // 2]
    return fit_aspect(median, policy)


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------
@dataclass
class VideoInfo:
    width: int
    height: int
    duration_s: float
    fps: float
    has_audio: bool

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def probe(path: Path) -> VideoInfo:
    if not shutil.which(FFPROBE):
        raise MediaError("ffprobe is not installed — cannot inspect video (brew install ffmpeg)")

    result = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MediaError(f"ffprobe could not read this video: {result.stderr.strip()[:200]}")

    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("no video stream found — is this actually a video file?")

    duration = float(payload.get("format", {}).get("duration") or video.get("duration") or 0.0)
    return VideoInfo(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        duration_s=duration,
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"),
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
    )


def _parse_fps(value: str) -> float:
    try:
        numerator, _, denominator = value.partition("/")
        return float(numerator) / float(denominator or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def normalise_video(data: bytes, policy: VideoPolicy, *, source_name: str = "") -> NormalisedMedia:
    """Re-encode to H.264/AAC MP4 satisfying `policy`."""
    if not shutil.which(FFMPEG):
        raise MediaError("ffmpeg is not installed — cannot normalise video (brew install ffmpeg)")

    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="postpilot-") as tmp:
        source = Path(tmp) / (source_name or "source")
        source.write_bytes(data)
        info = probe(source)

        # Duration is refused, never trimmed: a 4-minute video is not a
        # 90-second video with the end cut off.
        if info.duration_s < policy.min_duration_s:
            raise MediaError(
                f"{info.duration_s:.1f}s is shorter than {policy.platform.label}'s "
                f"{policy.min_duration_s:g}s minimum"
            )
        if info.duration_s > policy.max_duration_s:
            raise MediaError(
                f"{info.duration_s:.0f}s is longer than {policy.platform.label}'s "
                f"{policy.max_duration_s:g}s maximum — shorten the video"
            )

        target_aspect = policy.target_aspect or info.aspect
        width, height = _video_canvas(info, target_aspect, policy)
        if (width, height) != (info.width, info.height):
            notes.append(f"letterboxed {info.width}x{info.height} to {width}x{height}")

        fps = policy.target_fps
        if info.fps and info.fps < policy.fps_min:
            notes.append(f"source is {info.fps:.0f} fps, below {policy.platform.label}'s {policy.fps_min}")

        output = Path(tmp) / f"out.{policy.output_ext}"
        # scale+pad keeps the whole frame and adds bars, matching the image
        # policy's pad-don't-crop rule.
        video_filter = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"format=yuv420p"
        )
        # Inputs first. ffmpeg applies options to whichever file follows them,
        # so a second input declared after the output options is parsed as an
        # option *on the output* and fails.
        command = [FFMPEG, "-y", "-i", str(source)]
        if not info.has_audio:
            # Some surfaces reject a reel with no audio track at all; a silent
            # one is cheap insurance.
            command += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={policy.audio_rate}"]
            notes.append("added a silent audio track")

        command += [
            "-vf", video_filter,
            "-r", str(fps),
            "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
            "-b:v", policy.video_bitrate, "-maxrate", policy.video_bitrate, "-bufsize", "16M",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", policy.audio_bitrate, "-ar", str(policy.audio_rate), "-ac", "2",
        ]
        if not info.has_audio:
            command += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
        command += ["-movflags", "+faststart", str(output)]

        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output.exists():
            raise MediaError(f"ffmpeg failed: {result.stderr.strip()[-300:]}")

        encoded = output.read_bytes()

    if len(encoded) > policy.max_bytes:
        raise MediaError(
            f"encoded video is {len(encoded) / 1024 / 1024:.0f} MB, over "
            f"{policy.platform.label}'s {policy.max_bytes // 1024 // 1024} MB limit"
        )
    if policy.min_bytes and len(encoded) < policy.min_bytes:
        raise MediaError(f"encoded video is only {len(encoded) // 1024} KB, which {policy.platform.label} rejects")

    return NormalisedMedia(
        data=encoded,
        content_type=policy.output_mime,
        width=width,
        height=height,
        duration_s=info.duration_s,
        notes=notes,
    )


def _video_canvas(info: VideoInfo, target_aspect: float, policy: VideoPolicy) -> tuple[int, int]:
    """Even-numbered canvas at the target ratio, within the policy's bounds."""
    # Size the canvas from the source's LONG edge, not from whichever edge
    # happens to match the target orientation. Using the short edge turns a
    # 1920x1080 clip into a 608x1080 portrait canvas — technically 9:16, but
    # the picture inside ends up postage-stamp sized.
    source_long = max(info.width, info.height) or 1920
    long_edge = min(policy.max_edge or source_long, source_long)

    if target_aspect >= 1:
        width = long_edge
        height = round(width / target_aspect)
    else:
        height = long_edge
        width = round(height * target_aspect)

    if policy.min_width and width < policy.min_width:
        width = policy.min_width
        height = round(width / target_aspect)
    if policy.min_height and height < policy.min_height:
        height = policy.min_height
        width = round(height * target_aspect)

    # H.264 requires even dimensions.
    return (width + (width % 2), height + (height % 2))
