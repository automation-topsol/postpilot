"""`postpilot publish` — the run algorithm.

The order of operations is the safety property, so it is worth stating plainly:

1. `sync` — validate, assign IDs, reconcile `_State` with the Sheet.
2. Process the `Action` column, then clear it.
3. Expire stale leases — a `publishing` row older than the lease window means
   the run holding it died, so it becomes `unknown` (never a blind retry).
4. Reconcile every `unknown`, including the ones step 3 just created, so a
   crashed run is resolved on the *next* run rather than the one after.
5. Select what is due.
6. **Write the lease to `_State` BEFORE any API call**, and flush it to the
   Sheet. A crash after this leaves evidence; a crash before it leaves nothing
   to clean up.
7. Prepare media, publish, classify the result, write it immediately.
8. Roll up into the brand tab, append `_Log`.

Step 6 is what makes two concurrent runs safe: a `publishing` row younger than
the lease window belongs to someone else.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from postpilot.drive import DriveClient
from postpilot.logging import get_logger
from postpilot.media.store import MediaStore
from postpilot.models import (
    Brand,
    HumanAction,
    LogEntry,
    Platform,
    PlatformState,
    Post,
    PostType,
    StateRow,
    roll_up_status,
)
from postpilot.prepare import prepare_post_platform
from postpilot.publishers.base import (
    BrandCreds,
    PreparedPost,
    Publisher,
    PublishResult,
    PublishStatus,
    backoff_delay,
)
from postpilot.reconcile import ReconcileOutcome, reconcile
from postpilot.sheets.client import SheetClient
from postpilot.sheets.schema import BRAND_TOOL_COLUMNS, STATE_HEADERS, STATE_TAB, header_index
from postpilot.sheets.state import sort_key, state_to_row
from postpilot.sync import SyncResult, sync

log = get_logger(__name__)


@dataclass
class PublishAttempt:
    post_id: str
    brand_slug: str
    platform: Platform
    status: PublishStatus | None = None
    detail: str = ""
    remote_url: str = ""
    dry_run: bool = False


@dataclass
class PublishRun:
    attempts: list[PublishAttempt] = field(default_factory=list)
    actions_applied: list[str] = field(default_factory=list)
    leases_skipped: list[str] = field(default_factory=list)
    leases_expired: list[str] = field(default_factory=list)
    reconciled: ReconcileOutcome | None = None
    log_entries: list[LogEntry] = field(default_factory=list)
    sync_result: SyncResult | None = None

    def count(self, status: PublishStatus) -> int:
        return sum(a.status is status for a in self.attempts)

    @property
    def published(self) -> int:
        return self.count(PublishStatus.SUCCESS)


# --------------------------------------------------------------------------
# Action column
# --------------------------------------------------------------------------
def apply_actions(
    result: SyncResult,
    client: SheetClient,
    *,
    now: dt.datetime,
    write: bool = True,
) -> tuple[list[str], list[LogEntry]]:
    """Consume the `Action` column: act, then clear the cell.

    This is the whole recovery path for someone with no terminal, so it runs
    before selection — an `Action=retry` set five minutes ago must take effect
    on this run, not the next one.
    """
    applied: list[str] = []
    entries: list[LogEntry] = []

    for brand_sync in result.brands:
        tab = client.read(brand_sync.brand.slug)
        if tab is None:
            continue
        writer = client.writer(tab)
        action_col = header_index(tab.headers, "Action")

        for post in brand_sync.posts:
            if post.action is None:
                continue
            states = [
                result.states[(post.post_id, p)]
                for p in post.platforms
                if (post.post_id, p) in result.states
            ]
            for state in states:
                _apply_one(post.action, state, now)

            applied.append(f"{post.post_id}: {post.action.value}")
            entries.append(
                LogEntry(
                    timestamp=now,
                    brand_slug=brand_sync.brand.slug,
                    post_id=post.post_id,
                    action=f"action:{post.action.value}",
                    result="applied",
                    details=f"{len(states)} platform(s)",
                )
            )
            # Clearing the cell is what tells the teammate it was handled.
            if write and action_col is not None:
                writer.set_cell(post.row_number, action_col, "")
            post.action = None

        if write:
            client.flush(writer)

    return applied, entries


def _apply_one(action: HumanAction, state: StateRow, now: dt.datetime) -> None:
    if action is HumanAction.RETRY:
        state.state = PlatformState.SCHEDULED
        state.attempts = 0
        state.last_error = ""
        state.next_attempt_at = None
        state.attempt_id = ""
        state.started_at = None
    elif action is HumanAction.MARK_PUBLISHED:
        # The human looked at the platform and saw it. Trust them, and record
        # that nobody can produce a real remote ID after the fact.
        state.state = PlatformState.PUBLISHED
        state.remote_url = "manual"
        state.remote_id = state.remote_id or "manual"
        state.completed_at = now
        state.last_error = ""
    elif action is HumanAction.SKIP:
        state.state = PlatformState.SKIPPED
        state.last_error = ""
        state.next_attempt_at = None


# --------------------------------------------------------------------------
# Selection and leasing
# --------------------------------------------------------------------------
def expire_leases(result: SyncResult, *, now: dt.datetime, lease_minutes: int) -> list[str]:
    """Turn abandoned `publishing` rows into `unknown`, before reconciliation.

    A `publishing` row older than the lease window means the run holding it
    died. We cannot know whether it sent the request, so the row becomes
    `unknown` — never a retry. Doing this *before* reconciliation means a
    crashed run is resolved on the very next run rather than the one after.
    """
    expired: list[str] = []
    window = dt.timedelta(minutes=lease_minutes)

    for state in result.states.values():
        if state.state is not PlatformState.PUBLISHING:
            continue
        if now - (state.started_at or now) < window:
            continue
        state.state = PlatformState.UNKNOWN
        state.last_error = (
            f"a previous run held this since "
            f"{(state.started_at or now).isoformat(timespec='seconds')} and did not finish"
        )
        expired.append(f"{state.post_id}/{state.platform.value}")
    return expired


def select_due(
    result: SyncResult,
    *,
    now: dt.datetime,
    lease_minutes: int,
    only_brand: str | None = None,
    only_post: str | None = None,
) -> tuple[list[tuple[Brand, Post, StateRow]], list[str], list[str]]:
    """What to attempt this run, plus which leases another run still holds."""
    due: list[tuple[Brand, Post, StateRow]] = []
    skipped: list[str] = []
    expired: list[str] = []
    lease_window = dt.timedelta(minutes=lease_minutes)

    for brand_sync in result.brands:
        brand = brand_sync.brand
        if only_brand and brand.slug != only_brand:
            continue
        for post in brand_sync.posts:
            if only_post and post.post_id != only_post:
                continue
            if post.scheduled_at is None or post.scheduled_at > now:
                continue

            for platform in post.platforms:
                state = result.states.get((post.post_id, platform))
                if state is None:
                    continue

                if state.state is PlatformState.PUBLISHING:
                    # Anything stale was already turned into `unknown` by
                    # expire_leases, so a `publishing` row here belongs to a
                    # run that is still alive. Leaving it alone is what makes
                    # two concurrent workflows safe.
                    if now - (state.started_at or now) < lease_window:
                        skipped.append(f"{post.post_id}/{platform.value}")
                    continue

                if not state.state.is_due_candidate:
                    continue
                if state.next_attempt_at and state.next_attempt_at > now:
                    continue
                if not post.is_valid_for(platform):
                    continue

                due.append((brand, post, state))

    return due, skipped, expired


def lease(state: StateRow, now: dt.datetime) -> str:
    """Claim a row. Written to the Sheet BEFORE any API call."""
    attempt_id = uuid.uuid4().hex[:12]
    state.state = PlatformState.PUBLISHING
    state.attempt_id = attempt_id
    state.started_at = now
    state.last_attempt_at = now
    state.attempts += 1
    return attempt_id


def record_result(
    state: StateRow,
    result: PublishResult,
    *,
    now: dt.datetime,
    max_attempts: int,
    backoff_base: int,
) -> None:
    """Translate an adapter result into `_State`. Called immediately after."""
    state.last_attempt_at = now

    if result.status is PublishStatus.SUCCESS:
        state.state = PlatformState.PUBLISHED
        state.remote_id = result.remote_id
        state.remote_url = result.remote_url
        state.completed_at = now
        state.last_error = ""
        state.next_attempt_at = None
        state.attempt_id = ""
        return

    state.last_error = result.error[:500]

    if result.status is PublishStatus.PERMANENT_FAILURE:
        state.state = PlatformState.PERMANENT_FAILED
        state.next_attempt_at = None
        state.attempt_id = ""
        return

    if result.status is PublishStatus.RETRYABLE_FAILURE:
        if state.attempts >= max_attempts:
            state.state = PlatformState.PERMANENT_FAILED
            state.last_error = f"{result.error[:400]} (gave up after {state.attempts} attempts)"
            state.next_attempt_at = None
        else:
            state.state = PlatformState.RETRYABLE_FAILED
            state.next_attempt_at = now + backoff_delay(state.attempts, backoff_base)
        state.attempt_id = ""
        return

    # UNKNOWN: keep the attempt ID and any container ID — reconciliation needs
    # them, and a human reading _State needs to see which attempt is in doubt.
    state.state = PlatformState.UNKNOWN
    if result.container_id:
        state.remote_id = result.container_id
    state.next_attempt_at = None


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------
def publish(
    client: SheetClient,
    drive: DriveClient,
    store: MediaStore,
    publishers: dict[Platform, Publisher],
    creds: dict[str, BrandCreds],
    *,
    tz_name: str,
    now: dt.datetime | None = None,
    lease_minutes: int = 20,
    max_attempts: int = 3,
    backoff_base: int = 300,
    reconcile_window: int = 30,
    only_brand: str | None = None,
    only_post: str | None = None,
    dry_run: bool = False,
) -> PublishRun:
    now = now or dt.datetime.now(dt.UTC)
    run = PublishRun()

    # 1. sync
    result = sync(client, tz_name=tz_name, now=now, only_brand=only_brand, write=not dry_run, drive=drive)
    run.sync_result = result

    # 2. Action column
    applied, action_entries = apply_actions(result, client, now=now, write=not dry_run)
    run.actions_applied = applied
    run.log_entries.extend(action_entries)

    # 3. expire abandoned leases, so step 4 can resolve them this run
    run.leases_expired = expire_leases(result, now=now, lease_minutes=lease_minutes)

    # 4. reconciliation, before anything is leased
    captions = _captions(result)
    run.reconciled = reconcile(
        list(result.states.values()),
        publishers,
        creds,
        captions,
        now=now,
        window_minutes=reconcile_window,
        max_attempts=max_attempts,
    )
    run.log_entries.extend(run.reconciled.log_entries)

    # 5. selection
    due, skipped, _ = select_due(
        result, now=now, lease_minutes=lease_minutes, only_brand=only_brand, only_post=only_post
    )
    run.leases_skipped = skipped

    if not due:
        _finish(client, result, run, now=now, dry_run=dry_run)
        return run

    # 6. dry run stops here: the pipeline has run, but no lease is written
    if dry_run:
        for brand, post, state in due:
            run.attempts.append(
                PublishAttempt(post.post_id, brand.slug, state.platform, dry_run=True,
                               detail="would publish")
            )
        _finish(client, result, run, now=now, dry_run=True)
        return run

    # 7. publish, recording each result the moment it is known
    for brand, post, state in due:
        # Lease immediately before this row's own API call, not for the whole
        # batch up front. If the run dies partway, rows it never reached are
        # still `scheduled` rather than sitting leased for the lease window and
        # then needing reconciliation to discover nothing happened.
        lease(state, now)
        _write_state(client, result)

        attempt = PublishAttempt(post.post_id, brand.slug, state.platform)
        outcome = _publish_one(post, brand, state.platform, drive, store, publishers, creds, now=now)
        record_result(state, outcome, now=now, max_attempts=max_attempts, backoff_base=backoff_base)

        attempt.status = outcome.status
        attempt.detail = outcome.error or outcome.remote_id
        attempt.remote_url = outcome.remote_url
        run.attempts.append(attempt)
        run.log_entries.append(
            LogEntry(
                timestamp=now,
                brand_slug=brand.slug,
                post_id=post.post_id,
                platform=state.platform,
                action="publish",
                result=outcome.status.value,
                details=(outcome.error or outcome.remote_url or outcome.remote_id)[:400],
            )
        )
        # Flush after every platform: a crash must never lose a recorded
        # success, because a lost success is what causes a double publish.
        _write_state(client, result)

    _finish(client, result, run, now=now, dry_run=False)
    return run


def _publish_one(
    post: Post,
    brand: Brand,
    platform: Platform,
    drive: DriveClient,
    store: MediaStore,
    publishers: dict[Platform, Publisher],
    creds: dict[str, BrandCreds],
    *,
    now: dt.datetime,
) -> PublishResult:
    publisher = publishers.get(platform)
    if publisher is None:
        return PublishResult.permanent(f"no {platform.label} adapter is available yet")

    brand_creds = creds.get(brand.slug)
    if brand_creds is None or not brand_creds.token_for(platform):
        return PublishResult.permanent(f"no {platform.label} credentials configured for {brand.name}")

    media_urls: list[str] = []
    if post.post_type is not PostType.TEXT:
        resolution = drive.resolve(brand.drive_folder_id, post.media)
        prepared = prepare_post_platform(post, brand, platform, resolution, drive, store)
        if not prepared.ok:
            # Media problems are the row's fault, not the platform's, and no
            # amount of retrying fixes a missing file.
            return PublishResult.permanent("; ".join(prepared.errors))
        media_urls = prepared.urls

    payload = PreparedPost(post=post, media_urls=media_urls, caption=post.caption_for(platform, brand))

    try:
        return publisher.publish(payload, brand_creds)
    except Exception as exc:
        # An adapter that raises instead of classifying leaves us unable to say
        # whether the request was sent. Unknown is the only safe reading.
        log.exception("adapter %s raised", platform.value)
        return PublishResult.unknown(f"adapter raised {type(exc).__name__}: {exc}")


def _captions(result: SyncResult) -> dict[tuple[str, Platform], str]:
    out: dict[tuple[str, Platform], str] = {}
    for brand_sync in result.brands:
        for post in brand_sync.posts:
            for platform in post.platforms:
                out[(post.post_id, platform)] = post.caption_for(platform, brand_sync.brand)
    return out


def _write_state(client: SheetClient, result: SyncResult) -> None:
    rows = [state_to_row(s) for s in sorted(result.states.values(), key=sort_key)]
    client.replace_rows(STATE_TAB, STATE_HEADERS, rows)


def _finish(
    client: SheetClient, result: SyncResult, run: PublishRun, *, now: dt.datetime, dry_run: bool
) -> None:
    """Roll per-platform states back up into the brand tabs, and log."""
    if dry_run:
        return

    for brand_sync in result.brands:
        tab = client.read(brand_sync.brand.slug)
        if tab is None:
            continue
        writer = client.writer(tab)
        start = header_index(tab.headers, BRAND_TOOL_COLUMNS[0])
        if start is None:
            continue

        for post in brand_sync.posts:
            states = [
                result.states[(post.post_id, p)]
                for p in post.platforms
                if (post.post_id, p) in result.states
            ]
            status = roll_up_status(states, is_draft=post.is_draft, has_issues=bool(post.issues))
            brand_sync.statuses[post.post_id] = status

            errors = [str(i) for i in post.issues]
            errors += [f"{s.platform.value}: {s.last_error}" for s in states if s.last_error]
            writer.set_range(
                post.row_number,
                start,
                [
                    status.value,
                    "; ".join(f"{s.platform.value}: {s.remote_url}" for s in states if s.remote_url),
                    "; ".join(dict.fromkeys(errors))[:500],
                    " / ".join(f"{s.platform.value} {s.attempts}" for s in states),
                    now.isoformat(timespec="seconds"),
                    "; ".join(post.warnings)[:500],
                ],
            )
        client.flush(writer)

    _write_state(client, result)
    if run.log_entries:
        client.append_log(run.log_entries)
