"""`postpilot prepare` — get media onto R2, ready for the publishers.

    Drive metadata (id, md5) -> deterministic key -> HEAD -> exists? reuse : download -> normalise -> PUT

No local cache: the key is derived entirely from immutable inputs, so a fresh
GitHub Actions runner behaves exactly like a laptop that has run this before.

Each platform gets its own normalised output under its own key, because the
same source file legitimately becomes different files for different platforms —
Facebook Reels' 90-second, strictly-9:16 rules are not Instagram's.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from postpilot.drive import DriveClient, Resolution
from postpilot.logging import get_logger
from postpilot.media.normalise import (
    MediaError,
    NormalisedMedia,
    carousel_aspect,
    normalise_image,
    normalise_video,
)
from postpilot.media.policies import (
    ImagePolicy,
    MediaKind,
    PolicyNotAvailable,
    policy_for,
)
from postpilot.media.store import MediaStore, media_key
from postpilot.models import Brand, Platform, Post, PostType

log = get_logger(__name__)


@dataclass
class PreparedMedia:
    """Everything a publisher needs for one (post, platform)."""

    post_id: str
    brand_slug: str
    platform: Platform
    urls: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reused: int = 0
    uploaded: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def note_text(self) -> str:
        return "; ".join(dict.fromkeys(self.notes))


@dataclass
class PrepareResult:
    prepared: list[PreparedMedia] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def uploaded(self) -> int:
        return sum(p.uploaded for p in self.prepared)

    @property
    def reused(self) -> int:
        return sum(p.reused for p in self.prepared)

    @property
    def failed(self) -> list[PreparedMedia]:
        return [p for p in self.prepared if not p.ok]


def prepare_post_platform(
    post: Post,
    brand: Brand,
    platform: Platform,
    resolution: Resolution,
    drive: DriveClient,
    store: MediaStore,
    *,
    dry_run: bool = False,
) -> PreparedMedia:
    """Prepare every media item for one (post, platform)."""
    out = PreparedMedia(post_id=post.post_id, brand_slug=brand.slug, platform=platform)

    if post.post_type is PostType.TEXT:
        return out  # nothing to prepare; not an error

    if not resolution.ok:
        out.errors.extend(resolution.errors)
        return out

    try:
        policy = policy_for(platform, post.post_type)
    except PolicyNotAvailable as exc:
        out.errors.append(str(exc))
        return out

    files = resolution.files
    multi = len(files) > 1
    keys = [
        media_key(brand.slug, post.post_id, platform, policy, f.md5, index=i if multi else None)
        for i, f in enumerate(files)
    ]

    # Check every key first. If they are all present the whole post is already
    # prepared and nothing needs downloading — the common case on a re-run.
    try:
        present = [store.exists(key) for key in keys]
    except Exception as exc:
        out.errors.append(f"cannot reach media storage: {exc}")
        return out

    if all(present):
        out.keys = keys
        out.urls = [store.url_for(k) for k in keys]
        out.reused = len(keys)
        log.debug("%s/%s: all %d item(s) already prepared", post.post_id, platform.value, len(keys))
        return out

    # A carousel must share one aspect ratio across items, which can only be
    # decided once every item's dimensions are known.
    shared_aspect: float | None = None
    downloaded: dict[int, bytes] = {}

    if policy.kind is MediaKind.IMAGE and multi and isinstance(policy, ImagePolicy) and policy.uniform_aspect:
        import io

        from PIL import Image

        sizes: list[tuple[int, int]] = []
        for i, item in enumerate(files):
            downloaded[i] = drive.download(item.id)
            try:
                with Image.open(io.BytesIO(downloaded[i])) as probe_img:
                    sizes.append(probe_img.size)
            except Exception as exc:
                out.errors.append(f"{item.name!r}: cannot read this image ({exc})")
                return out
        shared_aspect = carousel_aspect(sizes, policy)
        out.notes.append(f"carousel unified to {shared_aspect:.2f}:1")

    for index, (item, key, exists) in enumerate(zip(files, keys, present, strict=True)):
        if exists:
            out.keys.append(key)
            out.urls.append(store.url_for(key))
            out.reused += 1
            continue

        if dry_run:
            out.keys.append(key)
            out.urls.append(store.url_for(key))
            out.notes.append(f"would upload {key}")
            continue

        try:
            data = downloaded.get(index) or drive.download(item.id)
            if policy.kind is MediaKind.IMAGE:
                result = normalise_image(data, policy, force_aspect=shared_aspect)  # type: ignore[arg-type]
            else:
                result = normalise_video(data, policy, source_name=item.name)  # type: ignore[arg-type]
        except MediaError as exc:
            out.errors.append(f"{item.name!r}: {exc}")
            return out
        except Exception as exc:
            out.errors.append(f"{item.name!r}: unexpected media failure: {exc}")
            return out

        out.notes.extend(_notes_for(item.name, result, policy))
        url = store.put(key, result.data, result.content_type)
        out.keys.append(key)
        out.urls.append(url)
        out.uploaded += 1

    return out


def _notes_for(name: str, result: NormalisedMedia, policy) -> list[str]:
    if not result.notes:
        return []
    label = policy.platform.label
    return [f"{label}/{name}: {note}" for note in result.notes]


def prepare(
    posts: list[tuple[Brand, Post]],
    drive: DriveClient,
    store: MediaStore,
    *,
    now: dt.datetime | None = None,
    lookahead_hours: int = 24,
    only_post: str | None = None,
    dry_run: bool = False,
) -> PrepareResult:
    """Prepare media for posts due now or within the lookahead window."""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now + dt.timedelta(hours=lookahead_hours)
    result = PrepareResult()

    for brand, post in posts:
        if only_post and post.post_id != only_post:
            continue
        if post.post_type is PostType.TEXT:
            continue
        if not only_post:
            if post.scheduled_at is None:
                result.skipped.append(f"{post.post_id}: no schedule")
                continue
            if post.scheduled_at > cutoff:
                continue

        resolution = drive.resolve(brand.drive_folder_id, post.media)
        for platform in post.platforms:
            # Preparing media for a platform the row cannot publish to is
            # wasted work and a misleading error. Skip it quietly; `sync`
            # already recorded why.
            if not post.is_valid_for(platform):
                continue
            result.prepared.append(
                prepare_post_platform(
                    post, brand, platform, resolution, drive, store, dry_run=dry_run
                )
            )

    return result
