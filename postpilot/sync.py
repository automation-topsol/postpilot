"""`postpilot sync` — read the Sheet, validate, assign IDs, reconcile `_State`.

Sync is deliberately side-effect-light: it never calls a publishing API and
never leases. It answers one question per (post, platform) — *should this be
publishable?* — and records the answer where `publish` can act on it.

The one subtle rule lives in `_reconcile_platform`: a changed content hash
re-opens a row that failed or was invalid, but does **nothing** to one that
already published. A corrected row retries itself; a published one is never
republished, it only earns a `Notes` warning.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from postpilot.models import (
    Brand,
    LogEntry,
    Platform,
    PlatformState,
    Post,
    RowStatus,
    StateRow,
    roll_up_status,
)
from postpilot.sheets.client import SheetClient, Tab
from postpilot.sheets.parse import parse_brands, parse_post
from postpilot.sheets.schema import (
    BRAND_TOOL_COLUMNS,
    BRANDS_TAB,
    COL_ID,
    COL_TOOL_START,
    STATE_HEADERS,
    STATE_TAB,
    header_index,
)
from postpilot.sheets.state import parse_state, sort_key, state_to_row

# States a hash change is allowed to re-open. `published` and `skipped` are
# deliberately absent: both are human-meaningful end states.
REOPENABLE = {
    PlatformState.INVALID,
    PlatformState.PERMANENT_FAILED,
    PlatformState.RETRYABLE_FAILED,
}


@dataclass
class BrandSync:
    """What sync worked out for one brand."""

    brand: Brand
    posts: list[Post] = field(default_factory=list)
    assigned_ids: int = 0
    reopened: int = 0
    statuses: dict[str, RowStatus] = field(default_factory=dict)


@dataclass
class SyncResult:
    brands: list[BrandSync] = field(default_factory=list)
    states: dict[tuple[str, Platform], StateRow] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log_entries: list[LogEntry] = field(default_factory=list)

    @property
    def post_count(self) -> int:
        return sum(len(b.posts) for b in self.brands)

    def counts(self) -> dict[RowStatus, int]:
        tally: dict[RowStatus, int] = {}
        for brand in self.brands:
            for status in brand.statuses.values():
                tally[status] = tally.get(status, 0) + 1
        return tally


def next_post_id(prefix: str, existing: set[str]) -> str:
    """`gi-0042`. Stable and never reused, because it is the key everywhere."""
    highest = 0
    for post_id in existing:
        head, _, tail = post_id.rpartition("-")
        if head == prefix and tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}-{highest + 1:04d}"


def sync(
    client: SheetClient,
    *,
    tz_name: str,
    now: dt.datetime | None = None,
    only_brand: str | None = None,
    write: bool = True,
) -> SyncResult:
    """Read every tab once, compute, then write once per tab."""
    now = now or dt.datetime.now(dt.UTC)
    result = SyncResult()

    brands_tab = client.read(BRANDS_TAB)
    if brands_tab is None:
        result.problems.append(f"{BRANDS_TAB} tab is missing — run `postpilot sheet init`")
        return result

    brands, problems, warnings = parse_brands(brands_tab.headers, brands_tab.rows)
    result.problems.extend(problems)
    result.warnings.extend(warnings)

    state_tab = client.read(STATE_TAB)
    states = parse_state(state_tab.headers, state_tab.rows) if state_tab else {}
    result.states = states

    live_keys: set[tuple[str, Platform]] = set()

    for brand in brands:
        if not brand.active:
            continue
        if only_brand and brand.slug != only_brand:
            continue

        tab = client.read(brand.slug)
        if tab is None:
            result.problems.append(f"brand {brand.slug!r} has no tab — run `postpilot sheet init`")
            continue

        brand_sync = _sync_brand(client, brand, tab, states, live_keys, tz_name, now, write=write)
        result.brands.append(brand_sync)
        result.log_entries.extend(_log_for(brand_sync, now))

    # Drop state for rows the teammate deleted outright. Their post IDs are
    # never reissued, so this cannot resurrect anything.
    orphaned = [key for key in states if key not in live_keys and (only_brand is None or states[key].brand_slug == only_brand)]
    for key in orphaned:
        states.pop(key)

    if write and (result.brands or orphaned):
        _write_state(client, states)

    return result


def _sync_brand(
    client: SheetClient,
    brand: Brand,
    tab: Tab,
    states: dict[tuple[str, Platform], StateRow],
    live_keys: set[tuple[str, Platform]],
    tz_name: str,
    now: dt.datetime,
    *,
    write: bool,
) -> BrandSync:
    out = BrandSync(brand=brand)
    writer = client.writer(tab)

    existing_ids = {
        row[COL_ID].strip()
        for row in tab.rows
        if len(row) > COL_ID and row[COL_ID].strip()
    }

    for index, row in enumerate(tab.rows):
        post = parse_post(brand, tab.headers, row, tab.row_number(index), tz_name)
        if post is None:
            continue

        # Assign an ID on first sight. This is the key used everywhere after,
        # so it is written back immediately and never changes.
        if not post.post_id:
            if post.is_draft:
                continue  # a blank-but-for-a-caption row is not a post yet
            post.post_id = next_post_id(brand.id_prefix(), existing_ids)
            existing_ids.add(post.post_id)
            out.assigned_ids += 1
            id_col = header_index(tab.headers, "ID")
            if write and id_col is not None:
                writer.set_cell(post.row_number, id_col, post.post_id)

        out.posts.append(post)

        platform_states: list[StateRow] = []
        if not post.is_draft:
            content_hash = post.content_hash()
            for platform in post.platforms:
                key = (post.post_id, platform)
                live_keys.add(key)
                state = states.get(key)
                state, reopened = _reconcile_platform(post, platform, state, content_hash, brand, now)
                states[key] = state
                platform_states.append(state)
                out.reopened += int(reopened)

        status = roll_up_status(
            platform_states,
            is_draft=post.is_draft,
            has_issues=bool(post.issues),
        )
        out.statuses[post.post_id] = status

        if write:
            _write_row(writer, tab, post, status, platform_states, now)

    if write:
        client.flush(writer)
    return out


def _reconcile_platform(
    post: Post,
    platform: Platform,
    state: StateRow | None,
    content_hash: str,
    brand: Brand,
    now: dt.datetime,
) -> tuple[StateRow, bool]:
    """Bring one (post, platform) row of `_State` up to date with the Sheet."""
    valid = post.is_valid_for(platform)

    if state is None:
        return (
            StateRow(
                post_id=post.post_id,
                brand_slug=brand.slug,
                platform=platform,
                state=PlatformState.SCHEDULED if valid else PlatformState.INVALID,
                content_hash=content_hash,
                last_error="" if valid else "; ".join(str(i) for i in post.issues_for(platform)),
            ),
            False,
        )

    state.brand_slug = brand.slug
    changed = bool(state.content_hash) and state.content_hash != content_hash
    reopened = False

    if state.state is PlatformState.PUBLISHED:
        # Never republish. The hash is NOT updated, so the warning persists
        # and stays true: what is live differs from what the Sheet now says.
        if changed:
            post.warnings.append(f"{platform.label}: published version differs from the Sheet")
        return state, False

    if state.state is PlatformState.SKIPPED:
        return state, False

    if not valid:
        state.state = PlatformState.INVALID
        state.last_error = "; ".join(str(i) for i in post.issues_for(platform))
        state.content_hash = content_hash
        return state, False

    if changed and state.state in REOPENABLE:
        # A corrected row retries itself — the whole point of the hash.
        state.state = PlatformState.SCHEDULED
        state.attempts = 0
        state.last_error = ""
        state.next_attempt_at = None
        reopened = True
    elif state.state is PlatformState.INVALID:
        # Was invalid, now validates: re-open even without a content change,
        # because the fix may have been in _Brands rather than the row.
        state.state = PlatformState.SCHEDULED
        state.attempts = 0
        state.last_error = ""
        reopened = True

    state.content_hash = content_hash
    return state, reopened


def _write_row(
    writer,
    tab: Tab,
    post: Post,
    status: RowStatus,
    states: list[StateRow],
    now: dt.datetime,
) -> None:
    """Write the six tool-owned columns as ONE contiguous range."""
    published_urls = "; ".join(f"{s.platform.value}: {s.remote_url}" for s in states if s.remote_url)
    attempts = " / ".join(f"{s.platform.value} {s.attempts}" for s in states) if states else ""

    errors = [str(i) for i in post.issues]
    errors += [f"{s.platform.value}: {s.last_error}" for s in states if s.last_error]
    notes = "; ".join(post.warnings)

    values = [
        status.value,
        published_urls,
        "; ".join(dict.fromkeys(errors))[:500],
        attempts,
        now.isoformat(timespec="seconds"),
        notes[:500],
    ]
    start = header_index(tab.headers, BRAND_TOOL_COLUMNS[0])
    writer.set_range(post.row_number, start if start is not None else COL_TOOL_START, values)


def _write_state(client: SheetClient, states: dict[tuple[str, Platform], StateRow]) -> None:
    rows = [state_to_row(s) for s in sorted(states.values(), key=sort_key)]
    client.replace_rows(STATE_TAB, STATE_HEADERS, rows)


def _log_for(brand_sync: BrandSync, now: dt.datetime) -> list[LogEntry]:
    entries: list[LogEntry] = []
    if brand_sync.assigned_ids:
        entries.append(
            LogEntry(
                timestamp=now,
                brand_slug=brand_sync.brand.slug,
                action="sync",
                result="ok",
                details=f"assigned {brand_sync.assigned_ids} new post ID(s)",
            )
        )
    if brand_sync.reopened:
        entries.append(
            LogEntry(
                timestamp=now,
                brand_slug=brand_sync.brand.slug,
                action="sync",
                result="reopened",
                details=f"{brand_sync.reopened} platform(s) returned to scheduled after a correction",
            )
        )
    return entries
