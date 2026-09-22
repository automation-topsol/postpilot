"""`postpilot status` — what a human needs to see, in one table."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from postpilot.models import Platform, PlatformState, RowStatus, StateRow
from postpilot.sync import SyncResult

# Ordered by how much they want attention, not alphabetically.
STATUS_STYLE: dict[RowStatus, str] = {
    RowStatus.NEEDS_REVIEW: "bold magenta",
    RowStatus.FAILED: "bold red",
    RowStatus.PARTIAL: "yellow",
    RowStatus.INVALID: "red",
    RowStatus.PUBLISHING: "cyan",
    RowStatus.SCHEDULED: "green",
    RowStatus.PUBLISHED: "dim green",
    RowStatus.DRAFT: "dim",
}

ATTENTION = (RowStatus.NEEDS_REVIEW, RowStatus.FAILED, RowStatus.PARTIAL, RowStatus.INVALID)


def render(result: SyncResult, *, tz_name: str, console: Console | None = None, limit: int = 60) -> None:
    console = console or Console()
    tz = ZoneInfo(tz_name)
    now = dt.datetime.now(dt.UTC)

    if result.problems:
        console.print("\n[bold red]Configuration problems[/bold red]")
        for problem in result.problems:
            console.print(f"  • {escape(problem)}", style="red")
    if result.warnings:
        console.print("\n[bold yellow]Incomplete configuration[/bold yellow]")
        for warning in result.warnings:
            console.print(f"  • {escape(warning)}", style="yellow")

    for brand_sync in result.brands:
        brand = brand_sync.brand
        platforms = ", ".join(p.value for p in brand.enabled_platforms) or "none"
        table = Table(
            title=escape(f"{brand.name}  ({brand.slug} · {platforms})"),
            title_justify="left",
            header_style="bold",
            expand=False,
        )
        table.add_column("ID", no_wrap=True)
        table.add_column("When", no_wrap=True)
        table.add_column("Type", no_wrap=True)
        table.add_column("Platforms", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Detail", overflow="fold", max_width=54)

        # Soonest first; drafts (no schedule) last.
        far_future = dt.datetime.max.replace(tzinfo=dt.UTC)
        posts = sorted(brand_sync.posts, key=lambda p: p.scheduled_at or far_future)

        for post in posts[:limit]:
            states = [
                result.states[(post.post_id, p)]
                for p in post.platforms
                if (post.post_id, p) in result.states
            ]
            status = brand_sync.statuses.get(post.post_id, RowStatus.DRAFT)
            when = (
                post.scheduled_at.astimezone(tz).strftime("%d %b %H:%M")
                if post.scheduled_at
                else "—"
            )
            if post.scheduled_at and post.scheduled_at <= now and status is RowStatus.SCHEDULED:
                when += " (due)"

            table.add_row(
                escape(post.post_id or "—"),
                when,
                post.post_type.value if post.post_type else "—",
                ",".join(p.value for p in post.platforms) or "—",
                f"[{STATUS_STYLE[status]}]{status.value}[/{STATUS_STYLE[status]}]",
                # Error text is data and routinely contains quoted file names
                # and brackets; rich must not try to read it as markup.
                escape(_detail(post, states)),
            )

        if len(posts) > limit:
            table.caption = f"showing {limit} of {len(posts)} rows"
        console.print(table)

    _summary(console, result)


def _detail(post, states: list[StateRow]) -> str:
    """The single most useful line about this row, not everything about it."""
    if issues := post.issues:
        return "; ".join(str(i) for i in issues[:3])
    errors = [f"{s.platform.value}: {s.last_error}" for s in states if s.last_error]
    if errors:
        return "; ".join(errors[:3])
    if urls := [f"{s.platform.value}: {s.remote_url}" for s in states if s.remote_url]:
        return "; ".join(urls)
    if post.warnings:
        return "; ".join(post.warnings[:2])
    return ""


def _summary(console: Console, result: SyncResult) -> None:
    counts = result.counts()
    if not counts:
        console.print("\n[dim]No posts found.[/dim]")
        return

    parts = [
        f"[{STATUS_STYLE[status]}]{counts[status]} {status.value}[/{STATUS_STYLE[status]}]"
        for status in RowStatus
        if counts.get(status)
    ]
    console.print("\n" + "   ".join(parts))

    needing = sum(counts.get(s, 0) for s in ATTENTION)
    if needing:
        console.print(
            f"[bold]{needing} row(s) need attention.[/bold] "
            "Fix the row, or set the Action column to retry / mark published / skip.",
        )

    unknown = [s for s in result.states.values() if s.state is PlatformState.UNKNOWN]
    if unknown:
        console.print(
            f"[bold magenta]{len(unknown)} platform(s) in `unknown`[/bold magenta] — "
            "PostPilot could not confirm delivery and will not retry automatically.",
        )


def upcoming(result: SyncResult, within: dt.timedelta, *, now: dt.datetime | None = None) -> list[tuple[str, str]]:
    """(brand, post_id) pairs due in the next window — used by `summary`."""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now + within
    out: list[tuple[str, str]] = []
    for brand_sync in result.brands:
        for post in brand_sync.posts:
            if post.scheduled_at and now <= post.scheduled_at <= cutoff:
                out.append((brand_sync.brand.slug, post.post_id))
    return out


def platform_breakdown(result: SyncResult) -> dict[Platform, dict[PlatformState, int]]:
    out: dict[Platform, dict[PlatformState, int]] = {}
    for state in result.states.values():
        bucket = out.setdefault(state.platform, {})
        bucket[state.state] = bucket.get(state.state, 0) + 1
    return out
