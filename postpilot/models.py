"""Pydantic models and enums for everything crossing a boundary.

The Sheet is edited by hand, so nothing coming out of it can be trusted to be
well-formed. Parsing lives here and is deliberately forgiving about *shape*
(whitespace, casing, "TRUE"/"yes") while being strict about *meaning* (a
carousel really does need 2-10 images).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from postpilot.apis import HASH_VERSION

# The Sheet is written in Asia/Karachi; everything internal is UTC.
SHEET_TZ = "Asia/Karachi"


class Platform(StrEnum):
    """Publishing targets. Sheet uses the short codes."""

    FB = "FB"
    IG = "IG"
    LI = "LI"

    @property
    def label(self) -> str:
        return {"FB": "Facebook", "IG": "Instagram", "LI": "LinkedIn"}[self.value]


class PostType(StrEnum):
    IMAGE = "image"
    CAROUSEL = "carousel"
    REEL = "reel"
    TEXT = "text"


class PlatformState(StrEnum):
    """Per-(post, platform) delivery state. Lives in `_State`, the real truth."""

    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    RETRYABLE_FAILED = "retryable_failed"
    PERMANENT_FAILED = "permanent_failed"
    UNKNOWN = "unknown"
    INVALID = "invalid"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        """States the scheduler will never act on again without a human."""
        return self in {PlatformState.PUBLISHED, PlatformState.SKIPPED, PlatformState.PERMANENT_FAILED}

    @property
    def is_due_candidate(self) -> bool:
        return self in {PlatformState.SCHEDULED, PlatformState.RETRYABLE_FAILED}


class RowStatus(StrEnum):
    """Human-facing roll-up shown on the brand tab. Never read back as truth."""

    DRAFT = "draft"
    INVALID = "invalid"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PARTIAL = "partial"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class HumanAction(StrEnum):
    """The `Action` dropdown — how a non-technical teammate recovers a row."""

    RETRY = "retry"
    MARK_PUBLISHED = "mark published"
    SKIP = "skip"


# --------------------------------------------------------------------------
# Media count rules — strict, per §2 of the brief.
# --------------------------------------------------------------------------
MEDIA_COUNTS: dict[PostType, tuple[int, int]] = {
    PostType.IMAGE: (1, 1),
    PostType.CAROUSEL: (2, 10),
    PostType.REEL: (1, 1),
    PostType.TEXT: (0, 0),
}

# Platforms that accept a post with no media at all.
TEXT_CAPABLE = {Platform.FB, Platform.LI}

# Platforms where a `Link` is appended to the caption. IG captions are not
# clickable, so a link there is noise.
LINK_CAPABLE = {Platform.FB, Platform.LI}


class Brand(BaseModel):
    """One row of `_Brands`."""

    name: str
    slug: str
    enabled_platforms: list[Platform] = Field(default_factory=list)
    facebook_page_id: str = ""
    instagram_user_id: str = ""
    linkedin_org_urn: str = ""
    drive_folder_id: str = ""
    default_hashtags: str = ""
    active: bool = True

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", v):
            raise ValueError(f"slug {v!r} must be lowercase letters, digits and hyphens")
        return v

    @property
    def token_env_var(self) -> str:
        """Meta Page tokens are per brand; LinkedIn's single token is not."""
        return "META_PAGE_TOKEN_" + self.slug.upper().replace("-", "_")

    def platform_target(self, platform: Platform) -> str:
        return {
            Platform.FB: self.facebook_page_id,
            Platform.IG: self.instagram_user_id,
            Platform.LI: self.linkedin_org_urn,
        }[platform]

    def id_prefix(self) -> str:
        """Short, stable prefix for post IDs, e.g. "Grand Invitation" -> `gi`.

        Uniqueness across brands is checked when the roster loads, not here —
        a prefix collision is a configuration error, not a per-brand one.
        """
        initials = "".join(word[0] for word in re.findall(r"[A-Za-z0-9]+", self.name))[:3]
        return (initials or self.slug[:3]).lower()


class ValidationIssue(BaseModel):
    """Why a row, or a row on one platform, cannot be published."""

    platform: Platform | None = None  # None = applies to the whole row
    message: str

    def __str__(self) -> str:
        return f"{self.platform.value}: {self.message}" if self.platform else self.message


class Post(BaseModel):
    """A parsed brand-tab row, keyed by `ID` and looked up by it everywhere."""

    post_id: str
    brand_slug: str
    row_number: int  # 1-based sheet row; for writing back only, NEVER a key

    scheduled_at: dt.datetime | None = None  # UTC
    raw_date: str = ""
    raw_time: str = ""

    platforms: list[Platform] = Field(default_factory=list)
    post_type: PostType | None = None
    media: list[str] = Field(default_factory=list)

    caption: str = ""
    caption_facebook: str = ""
    caption_instagram: str = ""
    caption_linkedin: str = ""
    link: str = ""
    action: HumanAction | None = None

    issues: list[ValidationIssue] = Field(default_factory=list)
    # Non-blocking remarks surfaced in the row's `Notes` — e.g. "read
    # 03/04/2026 as 3 April (day-first)". A warning never stops a publish;
    # it exists so a silent assumption becomes a visible one.
    warnings: list[str] = Field(default_factory=list)

    # -- captions -----------------------------------------------------------
    def caption_for(self, platform: Platform, brand: Brand) -> str:
        """Resolve the caption actually sent to one platform.

        Precedence: per-platform override, else the shared caption. A `Link` is
        appended for FB/LinkedIn only. `Default Hashtags` are appended for
        Instagram only, and only when the resolved IG caption has no `#` of its
        own — the teammate's own hashtags always win.
        """
        override = {
            Platform.FB: self.caption_facebook,
            Platform.IG: self.caption_instagram,
            Platform.LI: self.caption_linkedin,
        }[platform]
        text = (override or self.caption).strip()

        if platform in LINK_CAPABLE and self.link:
            text = f"{text}\n\n{self.link}".strip()

        if platform is Platform.IG and brand.default_hashtags and "#" not in text:
            text = f"{text}\n\n{brand.default_hashtags.strip()}".strip()

        return text

    # -- validity -----------------------------------------------------------
    def row_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.platform is None]

    def issues_for(self, platform: Platform) -> list[ValidationIssue]:
        """Row-level issues affect every platform; platform issues only one."""
        return [i for i in self.issues if i.platform in (None, platform)]

    def is_valid_for(self, platform: Platform) -> bool:
        return not self.issues_for(platform)

    @property
    def is_draft(self) -> bool:
        """No schedule at all — the teammate is still writing it."""
        return self.scheduled_at is None and not self.raw_date.strip()

    # -- hashing ------------------------------------------------------------
    def content_hash(self, media_fingerprint: str | None = None) -> str:
        """Fingerprint of everything that changes what would be published.

        `media_fingerprint` is the resolved Drive file IDs + md5s once Phase 2
        can supply them; until then the raw Media cell text stands in. Changing
        that input is what `HASH_VERSION` exists to make deliberate.

        A changed hash re-validates a failed/invalid row. It never republishes
        a published one — that only earns a Notes warning.
        """
        parts = [
            HASH_VERSION,
            self.brand_slug,
            self.scheduled_at.isoformat() if self.scheduled_at else "",
            ",".join(sorted(p.value for p in self.platforms)),
            self.post_type.value if self.post_type else "",
            media_fingerprint if media_fingerprint is not None else "|".join(self.media),
            self.caption,
            self.caption_facebook,
            self.caption_instagram,
            self.caption_linkedin,
            self.link,
        ]
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


class StateRow(BaseModel):
    """One row of `_State` — the source of truth for delivery."""

    post_id: str
    brand_slug: str
    platform: Platform
    state: PlatformState = PlatformState.SCHEDULED
    attempts: int = 0
    attempt_id: str = ""
    remote_id: str = ""
    remote_url: str = ""
    content_hash: str = ""
    last_error: str = ""
    started_at: dt.datetime | None = None
    last_attempt_at: dt.datetime | None = None
    next_attempt_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None

    @property
    def key(self) -> tuple[str, Platform]:
        return (self.post_id, self.platform)


class LogEntry(BaseModel):
    """One append-only row of `_Log`."""

    timestamp: dt.datetime
    brand_slug: str = ""
    post_id: str = ""
    platform: Platform | None = None
    action: str = ""
    result: str = ""
    details: str = ""


def roll_up_status(states: list[StateRow], *, is_draft: bool, has_issues: bool) -> RowStatus:
    """Collapse per-platform states into the one word the teammate sees.

    Order matters: the most alarming true thing wins, because this is the
    column someone scans down looking for problems.
    """
    if is_draft:
        return RowStatus.DRAFT
    if not states:
        return RowStatus.INVALID if has_issues else RowStatus.DRAFT

    kinds = {s.state for s in states}

    if PlatformState.UNKNOWN in kinds:
        return RowStatus.NEEDS_REVIEW
    if PlatformState.PUBLISHING in kinds:
        return RowStatus.PUBLISHING

    # `skipped` is a deliberate human decision, so it never drags the row down.
    live = kinds - {PlatformState.SKIPPED}
    if not live:
        return RowStatus.SCHEDULED

    failed = {PlatformState.PERMANENT_FAILED, PlatformState.RETRYABLE_FAILED}
    published = PlatformState.PUBLISHED in live
    # Things that will NOT go out as the row currently stands.
    blocked = live & (failed | {PlatformState.INVALID})
    pending = live & {PlatformState.SCHEDULED}

    if published and blocked:
        # Some platforms are live and others cannot go: the distinction that
        # matters most, because the fix is per platform.
        return RowStatus.PARTIAL
    if live <= {PlatformState.PUBLISHED}:
        return RowStatus.PUBLISHED
    if live & failed:
        return RowStatus.FAILED
    if PlatformState.INVALID in live:
        # Only `invalid` when nothing is still queued to go out.
        return RowStatus.SCHEDULED if pending else RowStatus.INVALID
    return RowStatus.SCHEDULED
