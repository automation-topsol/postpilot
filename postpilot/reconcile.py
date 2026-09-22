"""Resolve `unknown` states — the one place the tool reasons about uncertainty.

An `unknown` means the request left the machine and we never got a definitive
answer. Retrying blindly could publish twice; giving up strands a post. So we
go and *look*, and the distinction that makes this safe is:

- **A successful lookup that does not contain our post is evidence of absence.**
  The post is not there, so scheduling it again cannot duplicate anything.
- **A lookup that fails is not evidence of anything.** The state stays
  `unknown`, which rolls up to `needs_review`, and a human decides.

That is the only reading under which the guarantee survives: *if a remote API
result is ambiguous, PostPilot stops and asks a human instead of retrying.* A
negative result from a working API is not ambiguous.

Reconciliation runs once, at the start of a run, before anything is leased.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from postpilot.logging import get_logger
from postpilot.models import LogEntry, Platform, PlatformState, StateRow
from postpilot.publishers.base import BrandCreds, Publisher, RemotePost

log = get_logger(__name__)

# The shorter caption must be at least this long before a prefix match is
# trusted. Without a floor, a post captioned "Hi" matches anything beginning
# with "Hi" — and reconciliation would mark the wrong post published, losing
# ours and recording someone else's URL against it.
_MIN_PREFIX_CHARS = 25


@dataclass
class ReconcileOutcome:
    resolved_published: list[tuple[str, Platform]] = field(default_factory=list)
    returned_to_scheduled: list[tuple[str, Platform]] = field(default_factory=list)
    still_unknown: list[tuple[str, Platform]] = field(default_factory=list)
    log_entries: list[LogEntry] = field(default_factory=list)

    @property
    def touched(self) -> int:
        return len(self.resolved_published) + len(self.returned_to_scheduled) + len(self.still_unknown)


def normalise_caption(text: str) -> str:
    """Collapse whitespace and case so platform-side reformatting still matches."""
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def captions_match(ours: str, theirs: str) -> bool:
    """Is this the same post?

    Exact equality after normalisation, or one caption being a **whole prefix**
    of the other. The prefix case is what survives the platforms appending
    hashtags or truncating long text.

    Deliberately NOT a truncated-prefix comparison. Comparing only the first N
    characters makes "…Part ONE of our series" and "…Part TWO of our series"
    identical, which is exactly the shape a brand posting a series produces —
    and picking the wrong one records the wrong URL and loses a post.
    """
    a, b = normalise_caption(ours), normalise_caption(theirs)
    if not a or not b:
        return False
    if a == b:
        return True

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) < _MIN_PREFIX_CHARS:
        return False
    return longer.startswith(shorter)


def find_match(
    caption: str,
    attempted_at: dt.datetime,
    candidates: list[RemotePost],
    window: dt.timedelta,
) -> RemotePost | None:
    """Match on caption AND time. Either alone is too loose."""
    for candidate in candidates:
        if abs(candidate.created_at - attempted_at) > window:
            continue
        if captions_match(caption, candidate.caption):
            return candidate
    return None


def reconcile(
    states: list[StateRow],
    publishers: dict[Platform, Publisher],
    creds: dict[str, BrandCreds],
    captions: dict[tuple[str, Platform], str],
    *,
    now: dt.datetime | None = None,
    window_minutes: int = 30,
    max_attempts: int = 3,
) -> ReconcileOutcome:
    """Try to settle every `unknown` in `states`. Mutates them in place.

    `captions` doubles as the scope of the run: a state whose post is not in it
    is skipped untouched, because without its caption we cannot tell "not
    published" from "we did not look properly".
    """
    now = now or dt.datetime.now(dt.UTC)
    window = dt.timedelta(minutes=window_minutes)
    outcome = ReconcileOutcome()

    unknowns = [s for s in states if s.state is PlatformState.UNKNOWN]
    if not unknowns:
        return outcome

    # One lookup per (brand, platform), shared by every unknown in it.
    lookups: dict[tuple[str, Platform], list[RemotePost] | None] = {}

    for state in unknowns:
        # `captions` is the authoritative list of posts THIS run loaded. A
        # state outside it belongs to a brand we did not sync (a --brand run),
        # so we have no caption to match on — and "no match" would then look
        # like "not published" and reschedule something that may well be live.
        # Leaving it completely untouched is the only safe move; a full run
        # will settle it properly.
        post_key = (state.post_id, state.platform)
        if post_key not in captions:
            continue

        key = (state.brand_slug, state.platform)
        publisher = publishers.get(state.platform)
        brand_creds = creds.get(state.brand_slug)

        if publisher is None or brand_creds is None:
            _leave_unknown(state, outcome, now, "no adapter available to check this platform")
            continue

        if key not in lookups:
            since = (state.started_at or now) - window
            try:
                lookups[key] = publisher.find_recent(brand_creds, since)
            except Exception as exc:
                # Could not look. That is not evidence of absence.
                lookups[key] = None
                log.warning("reconcile: lookup failed for %s/%s: %s", key[0], key[1].value, exc)

        candidates = lookups[key]
        if candidates is None:
            _leave_unknown(state, outcome, now, "could not reach the platform to check")
            continue

        attempted_at = state.started_at or state.last_attempt_at or now
        caption = captions[post_key]
        if not caption.strip():
            # Matching is by caption + time. With no caption there is nothing
            # to match on, so a miss proves nothing and rescheduling could
            # publish twice. A person has to look.
            _leave_unknown(
                state, outcome, now,
                "this post has no caption, so it cannot be identified on the platform",
            )
            continue

        match = find_match(caption, attempted_at, candidates, window)

        if match is not None:
            _mark_published(state, match, outcome, now)
        else:
            _return_to_scheduled(state, outcome, now, max_attempts)

    return outcome


def _mark_published(state: StateRow, match: RemotePost, outcome: ReconcileOutcome, now: dt.datetime) -> None:
    state.state = PlatformState.PUBLISHED
    state.remote_id = match.remote_id
    state.remote_url = match.url
    state.completed_at = match.created_at or now
    state.last_error = ""
    state.attempt_id = ""
    outcome.resolved_published.append((state.post_id, state.platform))
    outcome.log_entries.append(
        LogEntry(
            timestamp=now,
            brand_slug=state.brand_slug,
            post_id=state.post_id,
            platform=state.platform,
            action="reconcile",
            result="published",
            details=f"found it on the platform: {match.remote_id}",
        )
    )


def _return_to_scheduled(
    state: StateRow, outcome: ReconcileOutcome, now: dt.datetime, max_attempts: int
) -> None:
    """The lookup worked and our post is not there, so it was never published."""
    if state.attempts >= max_attempts:
        # Out of attempts. Leaving it unknown would imply we are unsure; we are
        # not — it simply failed too many times.
        state.state = PlatformState.PERMANENT_FAILED
        state.last_error = f"not published after {state.attempts} attempt(s); giving up"
        outcome.still_unknown.append((state.post_id, state.platform))
        result = "permanent_failed"
    else:
        state.state = PlatformState.SCHEDULED
        state.attempt_id = ""
        state.started_at = None
        state.next_attempt_at = None
        state.last_error = "previous attempt left no trace on the platform; rescheduled"
        outcome.returned_to_scheduled.append((state.post_id, state.platform))
        result = "scheduled"

    outcome.log_entries.append(
        LogEntry(
            timestamp=now,
            brand_slug=state.brand_slug,
            post_id=state.post_id,
            platform=state.platform,
            action="reconcile",
            result=result,
            details="not found on the platform within the match window",
        )
    )


def _leave_unknown(state: StateRow, outcome: ReconcileOutcome, now: dt.datetime, why: str) -> None:
    state.last_error = f"needs a human: {why}"
    outcome.still_unknown.append((state.post_id, state.platform))
    outcome.log_entries.append(
        LogEntry(
            timestamp=now,
            brand_slug=state.brand_slug,
            post_id=state.post_id,
            platform=state.platform,
            action="reconcile",
            result="needs_review",
            details=why,
        )
    )
