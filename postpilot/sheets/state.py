"""`_State` serialisation — the real source of truth for delivery.

One row per `(Post ID, Platform)`. Hidden, machine-owned, and never read back
from the human-facing `Status` column. Datetimes are ISO-8601 UTC so the tab
stays readable when someone does unhide it to debug.
"""

from __future__ import annotations

import datetime as dt

from postpilot.models import Platform, PlatformState, StateRow
from postpilot.sheets.parse import clean
from postpilot.sheets.schema import STATE_HEADERS, header_index

StateKey = tuple[str, Platform]


def _iso(value: dt.datetime | None) -> str:
    return value.astimezone(dt.UTC).isoformat(timespec="seconds") if value else ""


def _parse_dt(value: str) -> dt.datetime | None:
    text = clean(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def parse_state(headers: list[str], rows: list[list[str]]) -> dict[StateKey, StateRow]:
    """Read `_State` into a dict keyed by (post_id, platform).

    Unparseable rows are dropped rather than crashing the run: `_State` is
    machine-written, so a bad row means corruption, and the safe response is
    to treat that (post, platform) as unknown-to-us and let `sync` recreate it
    as `scheduled` — never to abort and leave every other post unpublished.
    """
    index = {name: header_index(headers, name) for name in STATE_HEADERS}
    out: dict[StateKey, StateRow] = {}

    def cell(row: list[str], name: str) -> str:
        i = index.get(name)
        return clean(row[i]) if i is not None and i < len(row) else ""

    for row in rows:
        post_id = cell(row, "Post ID")
        if not post_id:
            continue
        try:
            platform = Platform(cell(row, "Platform").upper())
            state = PlatformState(cell(row, "State").casefold() or "scheduled")
        except ValueError:
            continue

        attempts_raw = cell(row, "Attempts")
        out[(post_id, platform)] = StateRow(
            post_id=post_id,
            brand_slug=cell(row, "Brand"),
            platform=platform,
            state=state,
            attempts=int(attempts_raw) if attempts_raw.isdigit() else 0,
            attempt_id=cell(row, "Attempt ID"),
            remote_id=cell(row, "Remote ID"),
            remote_url=cell(row, "Remote URL"),
            content_hash=cell(row, "Content Hash"),
            last_error=cell(row, "Last Error"),
            started_at=_parse_dt(cell(row, "Started At")),
            last_attempt_at=_parse_dt(cell(row, "Last Attempt At")),
            next_attempt_at=_parse_dt(cell(row, "Next Attempt At")),
            completed_at=_parse_dt(cell(row, "Completed At")),
        )
    return out


def state_to_row(state: StateRow) -> list[str]:
    """Serialise in `STATE_HEADERS` order."""
    return [
        state.post_id,
        state.brand_slug,
        state.platform.value,
        state.state.value,
        str(state.attempts),
        state.attempt_id,
        state.remote_id,
        state.remote_url,
        state.content_hash,
        state.last_error[:500],
        _iso(state.started_at),
        _iso(state.last_attempt_at),
        _iso(state.next_attempt_at),
        _iso(state.completed_at),
    ]


def sort_key(state: StateRow) -> tuple[str, str, str]:
    """Stable ordering so diffs between runs stay readable."""
    return (state.brand_slug, state.post_id, state.platform.value)
