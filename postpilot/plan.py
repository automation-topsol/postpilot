"""The weekly plan: one Markdown file describing a batch of posts.

The operator makes a week of material in other tools, writes (or has an AI
write) a plan in the fixed template below, and drops it into `postpilot ui`
together with the files. This module turns that Markdown into posts and
validates them — using the **same** `parse_post` the Sheet uses, by building
the exact row the importer will write. So the preview and `sync` can never
disagree about whether a post is valid.

Template::

    brand: grandinvitation          <- optional default for every post below

    ## Mon 2026-09-28 18:30
    brand: grandinvitation
    platforms: FB, IG
    type: carousel
    media: gold-1.png, gold-2.png
    link: https://example.com

    caption:
    Shared caption, any number of lines.

    caption ig:
    Instagram-only caption. #hashtags

    ---

Simple fields (`brand`, `platforms`, `type`, `media`, `link`, `date`, `time`)
are only recognised **before** the first caption, so a caption line that
happens to start with "link:" stays part of the caption.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from postpilot.models import Brand, Platform, Post, PostType
from postpilot.sheets.parse import parse_post
from postpilot.sheets.schema import brand_headers, header_index

SIMPLE_FIELDS = ("brand", "platforms", "type", "media", "link", "date", "time")
CAPTION_KEYS = {
    "caption": "caption",
    "caption fb": "caption_facebook",
    "caption facebook": "caption_facebook",
    "caption ig": "caption_instagram",
    "caption instagram": "caption_instagram",
    "caption li": "caption_linkedin",
    "caption linkedin": "caption_linkedin",
}
IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "tif", "tiff"}
VIDEO_EXTS = {"mp4", "mov", "m4v", "webm", "avi", "mkv"}

_HEADING = re.compile(r"^##\s+(.*)$")
_FIELD = re.compile(rf"^({'|'.join(SIMPLE_FIELDS)})\s*:\s*(.*)$", re.IGNORECASE)
_CAPTION = re.compile(
    r"^(caption(?:\s+(?:fb|facebook|ig|instagram|li|linkedin))?)\s*:\s*(.*)$", re.IGNORECASE
)
_DATE_IN_HEADING = re.compile(r"(\d{4}-\d{2}-\d{2})")
_TIME_IN_HEADING = re.compile(r"(\d{1,2}[:.]\d{2}(?:\s*[AaPp][Mm])?|\d{1,2}\s*[AaPp][Mm])")


@dataclass
class PlannedPost:
    """One post from the plan — the editable shape the UI round-trips."""

    heading: str = ""
    brand: str = ""
    date: str = ""
    time: str = ""
    platforms: str = ""
    type: str = ""
    media: list[str] = field(default_factory=list)
    link: str = ""
    caption: str = ""
    caption_facebook: str = ""
    caption_instagram: str = ""
    caption_linkedin: str = ""
    include: bool = True

    def sheet_values(self) -> dict[str, str]:
        """Column name -> cell text, exactly as the importer writes it."""
        return {
            "Date": self.date,
            "Time": self.time,
            "Platforms": self.platforms,
            "Type": self.type.lower(),
            "Media": ", ".join(self.media),
            "Caption": self.caption,
            "Caption (Facebook)": self.caption_facebook,
            "Caption (Instagram)": self.caption_instagram,
            "Caption (LinkedIn)": self.caption_linkedin,
            "Link": self.link,
        }

    def row_for(self, headers: list[str]) -> list[str]:
        """A row in the order of `headers` — the real tab's, which humans reorder."""
        values = self.sheet_values()
        row = [""] * len(headers)
        for name, value in values.items():
            index = header_index(headers, name)
            if index is not None:
                row[index] = value
        return row


@dataclass
class Review:
    """What the preview shows for one post."""

    post: PlannedPost
    errors: list[str] = field(default_factory=list)  # block the whole post
    platform_errors: dict[str, list[str]] = field(default_factory=dict)  # block one platform
    warnings: list[str] = field(default_factory=list)
    captions: dict[str, str] = field(default_factory=dict)  # final text per platform
    duplicate_of: str = ""  # existing post ID when already in the Sheet
    scheduled_at: dt.datetime | None = None

    @property
    def importable(self) -> bool:
        """Blocked only when *nothing* could publish, or it is a duplicate.

        A post invalid on IG but fine on FB still imports — the Sheet handles
        per-platform validity exactly as it does for a hand-typed row.
        """
        if self.errors or self.duplicate_of:
            return False
        platforms = [p for p in self.captions]
        return any(p not in self.platform_errors for p in platforms)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_plan(text: str) -> tuple[list[PlannedPost], list[str]]:
    """Markdown -> posts, plus problems that are not tied to one post."""
    problems: list[str] = []
    default_brand = ""
    posts: list[PlannedPost] = []
    current: PlannedPost | None = None
    caption_attr: str | None = None
    caption_lines: list[str] = []
    # Comments are for the humans (and AIs) writing the plan; the template's
    # own explanation contains "brand: ..." lines that must not be read.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    def close_caption() -> None:
        nonlocal caption_attr, caption_lines
        if current is not None and caption_attr is not None:
            setattr(current, caption_attr, "\n".join(caption_lines).strip())
        caption_attr, caption_lines = None, []

    def close_post() -> None:
        nonlocal current
        close_caption()
        if current is not None:
            posts.append(current)
        current = None

    for raw in text.splitlines():
        line = raw.rstrip()

        if match := _HEADING.match(line):
            close_post()
            heading = match.group(1).strip()
            current = PlannedPost(heading=heading, brand=default_brand)
            if date := _DATE_IN_HEADING.search(heading):
                current.date = date.group(1)
                rest = heading[date.end():]
                if time := _TIME_IN_HEADING.search(rest):
                    current.time = time.group(1).replace(".", ":").strip()
            continue

        if line.strip() == "---":
            close_post()
            continue

        if current is None:
            # Preamble: only a default brand means anything here.
            if (match := _FIELD.match(line.strip())) and match.group(1).lower() == "brand":
                default_brand = match.group(2).strip().lower()
            continue

        if match := _CAPTION.match(line.strip()):
            close_caption()
            key = re.sub(r"\s+", " ", match.group(1).lower())
            caption_attr = CAPTION_KEYS[key]
            caption_lines = [match.group(2)] if match.group(2).strip() else []
            continue

        if caption_attr is not None:
            caption_lines.append(line)
            continue

        if match := _FIELD.match(line.strip()):
            name, value = match.group(1).lower(), match.group(2).strip()
            if name == "media":
                current.media = [m.strip() for m in value.split(",") if m.strip()]
            elif name == "brand":
                current.brand = value.lower()
            else:
                setattr(current, name, value)

    close_post()

    for post in posts:
        post.time = _normalise_time(post.time)

    if not posts:
        problems.append("no posts found — each post starts with a '## YYYY-MM-DD HH:MM' heading")
    return posts, problems


def _normalise_time(text: str) -> str:
    """"6:00 PM" -> "18:00", so the Sheet and the browser's time picker agree.

    Anything unreadable is left as typed, and review reports it with the
    Sheet's own "unreadable time" message.
    """
    cleaned = text.strip().upper().replace(".", ":")
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            return dt.datetime.strptime(cleaned, fmt).strftime("%H:%M")
        except ValueError:
            continue
    return text


# --------------------------------------------------------------------------
# Review
# --------------------------------------------------------------------------
def media_kind(name: str) -> str:
    ext = Path(name).suffix.lower().lstrip(".")
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    return ""


def review_post(
    post: PlannedPost,
    brands: dict[str, Brand],
    available_files: set[str],
    tz_name: str,
    now: dt.datetime,
) -> Review:
    review = Review(post=post)

    brand = brands.get(post.brand)
    if not post.brand:
        review.errors.append("no brand — add 'brand: <slug>' to the post or the top of the plan")
        return review
    if brand is None:
        review.errors.append(f"unknown brand {post.brand!r} — known: {', '.join(sorted(brands))}")
        return review
    if not post.date:
        review.errors.append("no date — put 'YYYY-MM-DD HH:MM' in the heading")
        return review

    headers = brand_headers()
    parsed: Post | None = parse_post(brand, headers, post.row_for(headers), 0, tz_name)
    if parsed is None:  # an entirely blank post
        review.errors.append("the post is empty")
        return review

    review.scheduled_at = parsed.scheduled_at
    review.warnings.extend(parsed.warnings)
    for issue in parsed.issues:
        if issue.platform is None:
            review.errors.append(issue.message)
        else:
            review.platform_errors.setdefault(issue.platform.value, []).append(issue.message)

    # A plan imported late must not dump a week of posts out at once.
    if parsed.scheduled_at and parsed.scheduled_at <= now:
        review.errors.append(
            "time is in the past — change it (a few minutes from now posts on the next run)"
        )

    # The files have to be in this batch; Drive does not know them yet.
    for name in post.media:
        if name not in available_files:
            review.errors.append(f"file {name!r} is not in the files you dropped")

    kinds = {media_kind(name) for name in post.media}
    if "" in kinds:
        unknown = [n for n in post.media if not media_kind(n)]
        review.errors.append(f"not an image or video: {', '.join(unknown)}")
    if parsed.post_type in (PostType.IMAGE, PostType.CAROUSEL) and "video" in kinds:
        review.errors.append(f"{parsed.post_type.value} takes images only — use type 'reel' for video")
    if parsed.post_type is PostType.REEL and "image" in kinds:
        review.errors.append("reel takes one video, not an image")

    for platform in parsed.platforms:
        review.captions[platform.value] = parsed.caption_for(platform, brand)

    if mismatch := _weekday_mismatch(post):
        review.warnings.append(mismatch)

    return review


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekday_mismatch(post: PlannedPost) -> str:
    """AIs writing plans routinely pair "Mon" with a Tuesday date. Say which wins."""
    try:
        date = dt.date.fromisoformat(post.date)
    except ValueError:
        return ""
    words = re.findall(r"[a-z]+", post.heading.lower())
    named = next((w[:3] for w in words if w[:3] in _WEEKDAYS and len(w) >= 3), None)
    actual = _WEEKDAYS[date.weekday()]
    if named and named != actual:
        return (
            f"heading says {named.title()} but {post.date} is a {date:%A} — "
            f"the date is what counts"
        )
    return ""


def caption_key(brand: str, scheduled_at: dt.datetime | None, captions: list[str]) -> tuple:
    """What makes two posts 'the same post' for duplicate detection.

    The full caption, not a prefix: a series ("Part 1" / "Part 2") posted at
    the same hour must not collide — the lesson of adversarial defect #2.
    """
    text = " ".join(" ".join(c.split()).casefold() for c in captions if c)
    return (brand, scheduled_at, text)


def plan_key(post: PlannedPost, scheduled_at: dt.datetime | None) -> tuple:
    return caption_key(
        post.brand,
        scheduled_at,
        [post.caption, post.caption_facebook, post.caption_instagram, post.caption_linkedin],
    )


def existing_key(post: Post) -> tuple:
    return caption_key(
        post.brand_slug,
        post.scheduled_at,
        [post.caption, post.caption_facebook, post.caption_instagram, post.caption_linkedin],
    )


def review_plan(
    posts: list[PlannedPost],
    brands: dict[str, Brand],
    available_files: set[str],
    existing: dict[tuple, str],
    tz_name: str,
    now: dt.datetime | None = None,
) -> list[Review]:
    """Review every post, flagging duplicates of the Sheet *and* of each other."""
    now = now or dt.datetime.now(dt.UTC)
    reviews: list[Review] = []
    seen: dict[tuple, int] = {}
    for index, post in enumerate(posts):
        review = review_post(post, brands, available_files, tz_name, now)
        if review.scheduled_at is not None:
            key = plan_key(post, review.scheduled_at)
            if key in existing:
                review.duplicate_of = existing[key]
            elif key in seen:
                review.duplicate_of = f"post #{seen[key] + 1} in this plan"
            else:
                seen[key] = index
        reviews.append(review)
    return reviews


def platforms_of(review: Review) -> list[Platform]:
    return [Platform(p) for p in review.captions]
