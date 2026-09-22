"""Turn hand-typed Sheet cells into validated models.

Forgiving about shape, strict about meaning. Every rejection carries a message
the non-technical teammate can act on without a terminal, because the `Error`
column is the only channel they have.
"""

from __future__ import annotations

import datetime as dt
import re
from zoneinfo import ZoneInfo

from postpilot.models import (
    MEDIA_COUNTS,
    TEXT_CAPABLE,
    Brand,
    HumanAction,
    Platform,
    Post,
    PostType,
    ValidationIssue,
)
from postpilot.sheets.schema import header_index

TRUEISH = {"true", "yes", "y", "1", "on", "✓", "x"}
FALSEISH = {"false", "no", "n", "0", "off", ""}

# Accepted date shapes. ISO first — it is what the guide tells people to use
# and the only one that cannot be misread.
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y")
_TIME_FORMATS = ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p")

_DRIVE_ID = re.compile(r"(?:/d/|/file/d/|[?&]id=)([A-Za-z0-9_-]{20,})")


def clean(value: object) -> str:
    """Strip, and normalise the non-breaking spaces that paste from docs."""
    return str(value or "").replace("\xa0", " ").strip()


def parse_bool(value: object, *, default: bool = False) -> bool:
    text = clean(value).casefold()
    if text in TRUEISH:
        return True
    if text in FALSEISH:
        return False
    return default


def parse_platforms(value: object) -> tuple[list[Platform], list[str]]:
    """`"FB, IG"` -> platforms. Returns (platforms, unrecognised tokens)."""
    platforms: list[Platform] = []
    unknown: list[str] = []
    for token in re.split(r"[,\s/|]+", clean(value)):
        if not token:
            continue
        code = token.upper()
        try:
            platform = Platform(code)
        except ValueError:
            unknown.append(token)
            continue
        if platform not in platforms:  # tolerate "FB, FB"
            platforms.append(platform)
    return platforms, unknown


def parse_media(value: object) -> list[str]:
    """Split the Media cell. Order is carousel order, so it is preserved.

    Commas separate entries; file names may legitimately contain spaces, so
    only commas and newlines split. Drive links are reduced to their file ID.
    """
    items: list[str] = []
    for raw in re.split(r"[,\n]+", clean(value)):
        item = raw.strip()
        if not item:
            continue
        if match := _DRIVE_ID.search(item):
            items.append(match.group(1))
        else:
            items.append(item)
    return items


def parse_schedule(
    raw_date: str, raw_time: str, tz_name: str
) -> tuple[dt.datetime | None, str | None, str | None]:
    """Combine Date + Time in the Sheet's timezone, return UTC.

    Returns (utc_datetime, error, warning). A missing date means "draft", not
    an error — the teammate is still writing it.
    """
    date_text, time_text = clean(raw_date), clean(raw_time)
    if not date_text:
        return None, None, None

    warning: str | None = None
    parsed_date: dt.date | None = None

    for fmt in _DATE_FORMATS:
        try:
            parsed_date = dt.datetime.strptime(date_text, fmt).date()
            break
        except ValueError:
            continue

    # Slash/dot dates are ambiguous between day-first and month-first.
    # Pakistan writes day-first, so that is the reading — but a silent
    # assumption becomes a visible one via the warning.
    if parsed_date is None and (
        match := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})", date_text)
    ):
            a, b, year = (int(g) for g in match.groups())
            if year < 100:
                year += 2000
            if a > 12 and b <= 12:
                day, month = a, b
            elif b > 12 and a <= 12:
                day, month = b, a  # unambiguously month-first
            else:
                day, month = a, b
                warning = f"read date {date_text!r} as {day:02d}/{month:02d} (day-first); use YYYY-MM-DD to be sure"
            try:
                parsed_date = dt.date(year, month, day)
            except ValueError:
                return None, f"unreadable date {date_text!r}", None

    if parsed_date is None:
        return None, f"unreadable date {date_text!r} — use YYYY-MM-DD", None

    parsed_time = dt.time(0, 0)
    if time_text:
        for fmt in _TIME_FORMATS:
            try:
                parsed_time = dt.datetime.strptime(time_text.upper().replace(".", ""), fmt).time()
                break
            except ValueError:
                continue
        else:
            return None, f"unreadable time {time_text!r} — use HH:MM (24-hour)", warning

    local = dt.datetime.combine(parsed_date, parsed_time, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(dt.UTC), None, warning


# --------------------------------------------------------------------------
# _Brands
# --------------------------------------------------------------------------
def parse_brands(
    headers: list[str], rows: list[list[str]]
) -> tuple[list[Brand], list[str], list[str]]:
    """Parse `_Brands`. Returns (brands, problems, warnings).

    A *problem* is structurally wrong and must be fixed: a duplicate slug, a
    malformed slug, colliding ID prefixes. A *warning* is a known-incomplete
    configuration that the tool handles correctly — chiefly a platform enabled
    before its credentials exist, which is exactly where LinkedIn sits while
    access is pending. Conflating the two would make every run of a correctly
    configured Sheet exit non-zero.
    """
    brands: list[Brand] = []
    problems: list[str] = []
    warnings: list[str] = []

    def cell(row: list[str], name: str) -> str:
        index = header_index(headers, name)
        return clean(row[index]) if index is not None and index < len(row) else ""

    for number, row in enumerate(rows, start=2):
        if not any(clean(c) for c in row):
            continue  # blank spacer row
        slug = cell(row, "Slug")
        name = cell(row, "Brand Name") or slug
        if not slug:
            problems.append(f"_Brands row {number}: no Slug")
            continue

        platforms, unknown = parse_platforms(cell(row, "Enabled Platforms"))
        if unknown:
            problems.append(f"_Brands row {number} ({slug}): unknown platform(s) {', '.join(unknown)}")

        try:
            brand = Brand(
                name=name,
                slug=slug,
                enabled_platforms=platforms,
                facebook_page_id=cell(row, "Facebook Page ID"),
                instagram_user_id=cell(row, "Instagram User ID"),
                linkedin_org_urn=cell(row, "LinkedIn Org URN"),
                drive_folder_id=cell(row, "Drive Folder ID"),
                default_hashtags=cell(row, "Default Hashtags"),
                active=parse_bool(cell(row, "Active"), default=True),
            )
        except ValueError as exc:
            problems.append(f"_Brands row {number}: {exc}")
            continue

        # A platform with no target ID can never publish; saying so at load
        # time beats discovering it on the first due post.
        for platform in brand.enabled_platforms:
            if not brand.platform_target(platform):
                warnings.append(
                    f"{slug}: {platform.label} is enabled but has no ID/URN in _Brands — "
                    f"its rows cannot publish until that is filled in"
                )
        brands.append(brand)

    seen: dict[str, str] = {}
    for brand in brands:
        if clash := seen.get(brand.slug):
            problems.append(f"duplicate slug {brand.slug!r} (also {clash})")
        seen[brand.slug] = brand.name

    prefixes: dict[str, str] = {}
    for brand in brands:
        prefix = brand.id_prefix()
        if clash := prefixes.get(prefix):
            problems.append(
                f"brands {clash!r} and {brand.name!r} both derive post-ID prefix {prefix!r}; rename one"
            )
        prefixes[prefix] = brand.name

    return brands, problems, warnings


# --------------------------------------------------------------------------
# Brand tabs
# --------------------------------------------------------------------------
def parse_post(
    brand: Brand,
    headers: list[str],
    row: list[str],
    row_number: int,
    tz_name: str,
) -> Post | None:
    """Parse one brand-tab row. Returns None for a blank row."""

    def cell(name: str) -> str:
        index = header_index(headers, name)
        return clean(row[index]) if index is not None and index < len(row) else ""

    if not any(clean(c) for c in row):
        return None

    scheduled_at, date_error, date_warning = parse_schedule(cell("Date"), cell("Time"), tz_name)

    action: HumanAction | None = None
    if raw_action := cell("Action").casefold():
        for candidate in HumanAction:
            if candidate.value == raw_action:
                action = candidate
                break

    post = Post(
        post_id=cell("ID"),
        brand_slug=brand.slug,
        row_number=row_number,
        scheduled_at=scheduled_at,
        raw_date=cell("Date"),
        raw_time=cell("Time"),
        platforms=parse_platforms(cell("Platforms"))[0],
        post_type=_parse_type(cell("Type")),
        media=parse_media(cell("Media")),
        caption=cell("Caption"),
        caption_facebook=cell("Caption (Facebook)"),
        caption_instagram=cell("Caption (Instagram)"),
        caption_linkedin=cell("Caption (LinkedIn)"),
        link=cell("Link"),
        action=action,
    )
    if date_warning:
        post.warnings.append(date_warning)

    validate(post, brand, headers, row, date_error)
    return post


def _parse_type(value: str) -> PostType | None:
    text = value.casefold().strip()
    for candidate in PostType:
        if candidate.value == text:
            return candidate
    return None


def validate(post: Post, brand: Brand, headers: list[str], row: list[str], date_error: str | None) -> None:
    """Attach issues. Row-level issues block every platform; platform-level
    issues block only that one — `text` + IG must never stop FB publishing."""
    issues = post.issues

    if date_error:
        issues.append(ValidationIssue(message=date_error))

    if post.is_draft:
        return  # nothing else is worth complaining about yet

    raw_type = _cell(headers, row, "Type")
    if post.post_type is None:
        issues.append(
            ValidationIssue(message=f"Type {raw_type!r} is not one of: " + ", ".join(t.value for t in PostType))
            if raw_type
            else ValidationIssue(message="Type is empty")
        )

    _, unknown = parse_platforms(_cell(headers, row, "Platforms"))
    if unknown:
        issues.append(ValidationIssue(message=f"unknown platform(s): {', '.join(unknown)}"))
    if not post.platforms:
        issues.append(ValidationIssue(message="Platforms is empty"))

    for platform in post.platforms:
        if platform not in brand.enabled_platforms:
            issues.append(
                ValidationIssue(
                    platform=platform,
                    message=f"{platform.label} is not in this brand's Enabled Platforms",
                )
            )
        elif not brand.platform_target(platform):
            issues.append(
                ValidationIssue(
                    platform=platform,
                    message=(
                        f"brand setting: {brand.name} has no {platform.label} ID/URN in _Brands "
                        f"(nothing wrong with this row)"
                    ),
                )
            )

    if post.post_type is not None:
        low, high = MEDIA_COUNTS[post.post_type]
        count = len(post.media)
        if not (low <= count <= high):
            expected = f"exactly {low}" if low == high else f"{low}-{high}"
            issues.append(
                ValidationIssue(message=f"{post.post_type.value} needs {expected} media file(s), found {count}")
            )

        # Per-platform capability, so one platform's limit never blocks another.
        if post.post_type is PostType.TEXT:
            for platform in post.platforms:
                if platform not in TEXT_CAPABLE:
                    issues.append(
                        ValidationIssue(platform=platform, message=f"{platform.label} cannot post without media")
                    )

    if not any((post.caption, post.caption_facebook, post.caption_instagram, post.caption_linkedin)):
        if post.post_type is PostType.TEXT:
            issues.append(ValidationIssue(message="a text post needs a Caption"))
        else:
            post.warnings.append("no caption — posting media with no text")

    if post.link and not re.match(r"https?://", post.link):
        issues.append(ValidationIssue(message=f"Link {post.link!r} must start with http:// or https://"))


def _cell(headers: list[str], row: list[str], name: str) -> str:
    index = header_index(headers, name)
    return clean(row[index]) if index is not None and index < len(row) else ""
