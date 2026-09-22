"""`postpilot` — the command line surface.

Commands that are not built yet exit with a clear "phase N" message rather
than a traceback or, worse, a silent success.
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.markup import escape

from postpilot import __version__
from postpilot.config import ENV_PATH, REPO_ROOT, MissingSetting, Settings, write_default_config
from postpilot.doctor import Level
from postpilot.doctor import run as run_doctor
from postpilot.drive import DriveClient
from postpilot.logging import configure
from postpilot.media.store import R2Store
from postpilot.prepare import prepare as run_prepare
from postpilot.publish import publish as run_publish
from postpilot.publishers.base import BrandCreds, PublishStatus
from postpilot.publishers.registry import available_publishers
from postpilot.sheets.client import SheetClient
from postpilot.sheets.setup import initialise
from postpilot.status import render
from postpilot.sync import sync as run_sync

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Publish scheduled posts from a Google Sheet to Facebook, Instagram and LinkedIn.",
)
sheet_app = typer.Typer(no_args_is_help=True, help="Create and repair the Sheet's tabs.")
app.add_typer(sheet_app, name="sheet")

console = Console()


def _settings(verbose: bool = False) -> Settings:
    configure(verbose=verbose)
    return Settings.load()


def _client(settings: Settings) -> SheetClient:
    return SheetClient(settings.google_credentials, settings.sheet_id)


def _fail(message: str) -> None:
    console.print(f"[bold red]✘[/bold red] {escape(message)}")
    raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def _root(version: bool = typer.Option(False, "--version", help="Show the version and exit.")) -> None:
    if version:
        console.print(f"postpilot {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------------
@app.command()
def init() -> None:
    """Write `config.yaml` and check that `.env` exists."""
    path = write_default_config()
    console.print(f"[green]✔[/green] {path.relative_to(REPO_ROOT)}")

    if ENV_PATH.exists():
        console.print("[green]✔[/green] .env present")
    else:
        console.print("[yellow]![/yellow] no .env — copy .env.example to .env and fill it in")

    console.print("\nNext: [bold]postpilot sheet init[/bold]")


@sheet_app.command("init")
def sheet_init(
    brand: str = typer.Option(None, "--brand", help="Only repair this brand's tab."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Create or repair tabs, headers, dropdowns, protections; hide `_State`."""
    settings = _settings(verbose)
    try:
        client = _client(settings)
        report = initialise(client, only_brand=brand)
    except MissingSetting as exc:
        _fail(str(exc))
        return

    console.print(f"[bold]{client.title}[/bold]")
    if report.created:
        console.print(f"[green]✔[/green] created: {', '.join(report.created)}")
    if report.repaired:
        console.print(f"[green]✔[/green] verified: {', '.join(report.repaired)}")
    for note in report.notes:
        console.print(f"[yellow]![/yellow] {escape(note)}")
    for problem in report.problems:
        console.print(f"[red]✘[/red] {escape(problem)}")
    if report.problems:
        raise typer.Exit(code=1)


@app.command()
def sync(
    brand: str = typer.Option(None, "--brand", help="Only sync this brand."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute everything, write nothing."),
    skip_media: bool = typer.Option(
        False,
        "--skip-media",
        help="Inspect without Drive. Requires --dry-run, because it changes the content hash.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Validate rows, assign IDs, compute hashes, reconcile `_State`."""
    if skip_media and not dry_run:
        # Without Drive the hash falls back to the raw Media text, so every
        # hash differs from the stored one — which would re-open every failed
        # and invalid row across the whole Sheet, resetting their attempts.
        # Harmless to published rows, but a nasty surprise, so it is refused
        # rather than silently allowed.
        _fail(
            "--skip-media changes the content hash, which would re-open every failed row. "
            "Add --dry-run to inspect without writing."
        )

    settings = _settings(verbose)
    try:
        client = _client(settings)
        result = run_sync(
            client,
            tz_name=settings.tunables.timezone,
            only_brand=brand,
            write=not dry_run,
            drive=None if skip_media else DriveClient(settings.google_credentials),
        )
    except MissingSetting as exc:
        _fail(str(exc))
        return

    if not dry_run and result.log_entries:
        client.append_log(result.log_entries)

    for problem in result.problems:
        console.print(f"[red]✘[/red] {escape(problem)}")
    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {escape(warning)}")

    assigned = sum(b.assigned_ids for b in result.brands)
    reopened = sum(b.reopened for b in result.brands)
    console.print(
        f"[green]✔[/green] {result.post_count} post(s) across {len(result.brands)} brand(s)"
        + (f" · {assigned} new ID(s)" if assigned else "")
        + (f" · {reopened} re-opened" if reopened else "")
        + (" [dim](dry run — nothing written)[/dim]" if dry_run else "")
    )
    counts = result.counts()
    if counts:
        console.print("  " + "  ".join(f"{v} {k.value}" for k, v in counts.items()))
    if result.problems:
        raise typer.Exit(code=1)


@app.command()
def status(
    brand: str = typer.Option(None, "--brand", help="Only this brand."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show upcoming, published, failed and needs-review rows."""
    settings = _settings(verbose)
    try:
        client = _client(settings)
        # Read-only: status must never change what publish would do.
        result = run_sync(
            client,
            tz_name=settings.tunables.timezone,
            only_brand=brand,
            write=False,
            drive=DriveClient(settings.google_credentials),
        )
    except MissingSetting as exc:
        _fail(str(exc))
        return
    render(result, tz_name=settings.tunables.timezone, console=console)


# --------------------------------------------------------------------------
# Not yet built. Explicit, so a missing feature never looks like a no-op.
# --------------------------------------------------------------------------
def _not_yet(command: str, phase: int) -> None:
    console.print(f"[yellow]![/yellow] `{command}` arrives in Phase {phase}. See CLAUDE.md §6.")
    raise typer.Exit(code=2)


@app.command()
def prepare(
    post: str = typer.Option(None, "--post", help="Only this post ID; ignores the schedule."),
    brand: str = typer.Option(None, "--brand", help="Only this brand."),
    hours: int = typer.Option(None, "--hours", help="Look this far ahead (default: config.yaml)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be uploaded, upload nothing."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Download, normalise and upload media for due and upcoming posts."""
    settings = _settings(verbose)
    try:
        client = _client(settings)
        drive = DriveClient(settings.google_credentials)
        store = R2Store.from_settings(settings)
        result = run_sync(
            client, tz_name=settings.tunables.timezone, only_brand=brand, write=False, drive=drive
        )
        pairs = [(b.brand, p) for b in result.brands for p in b.posts]
        if post and not any(p.post_id == post for _, p in pairs):
            # Silently reporting "nothing to do" for a typo'd ID is the kind
            # of no-op that gets mistaken for success.
            _fail(f"no post with ID {post!r}" + (f" in brand {brand!r}" if brand else ""))
        prepared = run_prepare(
            pairs,
            drive,
            store,
            lookahead_hours=hours if hours is not None else settings.tunables.prepare_lookahead_hours,
            only_post=post,
            dry_run=dry_run,
        )
    except MissingSetting as exc:
        _fail(str(exc))
        return

    for item in prepared.prepared:
        marker = "[green]✔[/green]" if item.ok else "[red]✘[/red]"
        console.print(f"{marker} {item.post_id} {item.platform.value}: "
                      f"{item.uploaded} uploaded, {item.reused} reused")
        for note in dict.fromkeys(item.notes):
            console.print(f"    [dim]{escape(note)}[/dim]")
        for error in item.errors:
            console.print(f"    [red]{escape(error)}[/red]")

    if not prepared.prepared:
        console.print("[dim]Nothing due to prepare.[/dim]")
    else:
        console.print(
            f"\n[bold]{prepared.uploaded} uploaded, {prepared.reused} reused[/bold]"
            + (" [dim](dry run)[/dim]" if dry_run else "")
        )
    if prepared.failed:
        raise typer.Exit(code=1)


@app.command()
def publish(
    dry_run: bool = typer.Option(False, "--dry-run", help="Run the whole pipeline, send nothing."),
    live: bool = typer.Option(False, "--live", help="Actually publish. Requires --confirm."),
    confirm: bool = typer.Option(False, "--confirm", help="Second half of the live-run safety gate."),
    brand: str = typer.Option(None, "--brand"),
    post: str = typer.Option(None, "--post"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Publish everything that is due.

    Local live runs need BOTH --live and --confirm: GitHub Actions is meant to
    be the only routine publisher, and two schedulers racing is exactly what
    the lease exists to survive rather than something to invite.
    """
    if live and not confirm:
        _fail("--live also requires --confirm. Nothing was sent.")
    if not live and not dry_run:
        _fail("pass --dry-run to rehearse, or --live --confirm to publish for real.")

    settings = _settings(verbose)
    try:
        client = _client(settings)
        drive = DriveClient(settings.google_credentials)
        store = R2Store.from_settings(settings)
        publishers = available_publishers()
        creds = _brand_creds(settings, client)

        if post:
            probe = run_sync(
                client, tz_name=settings.tunables.timezone, only_brand=brand, write=False, drive=drive
            )
            if not any(p.post_id == post for b in probe.brands for p in b.posts):
                _fail(f"no post with ID {post!r}" + (f" in brand {brand!r}" if brand else ""))

        result = run_publish(
            client,
            drive,
            store,
            publishers,
            creds,
            tz_name=settings.tunables.timezone,
            lease_minutes=settings.tunables.lease_minutes,
            max_attempts=settings.tunables.max_attempts,
            backoff_base=settings.tunables.backoff_base_seconds,
            reconcile_window=settings.tunables.reconcile_window_minutes,
            only_brand=brand,
            only_post=post,
            dry_run=not live,
        )
    except MissingSetting as exc:
        _fail(str(exc))
        return

    if not publishers:
        console.print("[yellow]![/yellow] no platform adapters are built yet (Facebook lands in Phase 4)")

    for item in result.actions_applied:
        console.print(f"[cyan]→[/cyan] action {escape(item)}")
    if result.reconciled:
        for post_id, platform in result.reconciled.resolved_published:
            console.print(f"[green]✔[/green] reconciled {escape(post_id)} {platform.value}: already published")
        for post_id, platform in result.reconciled.returned_to_scheduled:
            console.print(f"[yellow]↻[/yellow] reconciled {escape(post_id)} {platform.value}: not found, rescheduled")
        for post_id, platform in result.reconciled.still_unknown:
            console.print(f"[magenta]?[/magenta] {escape(post_id)} {platform.value}: needs a human")
    for item in result.leases_skipped:
        console.print(f"[dim]… {escape(item)} is leased by another run — skipped[/dim]")
    for item in result.leases_expired:
        console.print(f"[yellow]![/yellow] {escape(item)}: a previous run did not finish")

    glyph = {
        PublishStatus.SUCCESS: "[green]✔[/green]",
        PublishStatus.PERMANENT_FAILURE: "[red]✘[/red]",
        PublishStatus.RETRYABLE_FAILURE: "[yellow]↻[/yellow]",
        PublishStatus.UNKNOWN: "[magenta]?[/magenta]",
    }
    for attempt in result.attempts:
        mark = "[dim]·[/dim]" if attempt.dry_run else glyph[attempt.status]
        console.print(
            f"{mark} {escape(attempt.post_id)} {attempt.platform.value}: "
            f"{escape(attempt.detail or (attempt.status.value if attempt.status else ''))}"
        )

    if not result.attempts:
        console.print("[dim]Nothing is due.[/dim]")
    else:
        console.print(
            f"\n[bold]{result.published} published[/bold]"
            + (" [dim](dry run — nothing was sent)[/dim]" if not live else "")
        )
    if any(a.status is PublishStatus.PERMANENT_FAILURE for a in result.attempts):
        raise typer.Exit(code=1)


def _brand_creds(settings: Settings, client) -> dict[str, BrandCreds]:
    """Credentials per brand. A brand with no token simply gets none — the
    run algorithm turns that into a clear permanent failure, not a crash."""
    from postpilot.sheets.parse import parse_brands
    from postpilot.sheets.schema import BRANDS_TAB

    tab = client.read(BRANDS_TAB)
    if tab is None:
        return {}
    brands, _, _ = parse_brands(tab.headers, tab.rows)

    out: dict[str, BrandCreds] = {}
    for item in brands:
        out[item.slug] = BrandCreds(
            brand=item,
            meta_page_token=settings.meta_page_token(item.slug) if settings.has_meta_token(item.slug) else "",
            linkedin_access_token=settings.linkedin_access_token if settings.has_linkedin else "",
        )
    return out


@app.command()
def summary(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    to_log: bool = typer.Option(False, "--to-log", help="Skip Telegram and write to _Log."),
) -> None:
    """Send the daily digest to Telegram, or write it to `_Log` if that fails.

    The digest is the only routine signal that the scheduler is alive, so it is
    never silently skipped: if Telegram is unconfigured or refuses, the same
    text goes to the `_Log` tab instead.
    """
    from postpilot.summary import build_digest, render_text, send, to_log_entries

    settings = _settings(verbose)
    try:
        client = _client(settings)
        result = run_sync(
            client,
            tz_name=settings.tunables.timezone,
            write=False,
            drive=DriveClient(settings.google_credentials),
        )
    except MissingSetting as exc:
        _fail(str(exc))
        return

    digest = build_digest(result, settings)
    text = render_text(digest, settings.tunables.timezone)
    console.print(escape(text))

    delivered, detail = (False, "skipped by --to-log") if to_log else send(
        digest, settings, tz_name=settings.tunables.timezone
    )

    if delivered:
        console.print(f"\n[green]✔[/green] {escape(detail)}")
        return

    reason = "unconfigured" if not settings.has_telegram else "fallback"
    client.append_log(to_log_entries(digest, settings.tunables.timezone, reason=reason))
    console.print(f"\n[yellow]![/yellow] {escape(detail)} — written to the _Log tab instead")


@app.command()
def doctor(
    offline: bool = typer.Option(False, "--offline", help="Only check local tooling."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Check every credential and external dependency."""
    settings = _settings(verbose)
    report = run_doctor(settings, skip_network=offline)

    glyph = {Level.PASS: "[green]✔[/green]", Level.WARN: "[yellow]![/yellow]", Level.FAIL: "[red]✘[/red]"}
    for finding in report.findings:
        # escape(): a finding name like "Drive [grandinvitation]" is data, and
        # rich would otherwise read the brackets as markup and swallow them.
        name = escape(finding.name)
        detail = escape(finding.detail)
        console.print(f"{glyph[finding.level]} {name}" + (f" — {detail}" if detail else ""))
        if finding.fix:
            console.print(f"    [dim]fix: {escape(finding.fix)}[/dim]")

    counts = report.counts()
    console.print(
        f"\n[bold]{counts[Level.PASS]} ok · {counts[Level.WARN]} warning(s) · {counts[Level.FAIL]} failure(s)[/bold]"
    )
    if report.failed:
        raise typer.Exit(code=1)


auth_app = typer.Typer(no_args_is_help=True, help="Guided token acquisition.")
app.add_typer(auth_app, name="auth")


@auth_app.command("meta")
def auth_meta(brand: str = typer.Option(..., "--brand")) -> None:
    """Exchange a short-lived user token for long-lived Page tokens. (Phase 4)"""
    console.print("[yellow]![/yellow] `auth meta` arrives in Phase 4.")
    console.print("[dim]Working prototype: uv run python spike/meta_exchange_token.py --help[/dim]")
    raise typer.Exit(code=2)


@auth_app.command("linkedin")
def auth_linkedin(
    refresh_only: bool = typer.Option(
        False, "--refresh", help="Use the stored refresh token instead of a browser round trip."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Acquire or refresh the single LinkedIn token.

    Takes no --brand: one token covers every Company Page the operator
    administers. Only the org URN is per brand, and it lives in _Brands.
    """
    import os

    from postpilot.auth.linkedin import authorize
    from postpilot.auth.linkedin import refresh as refresh_token_flow

    settings = _settings(verbose)
    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "").strip()
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        _fail("LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be set in .env")

    stored_refresh = os.environ.get("LINKEDIN_REFRESH_TOKEN", "").strip()
    try:
        if refresh_only or (stored_refresh and settings.linkedin_expires_at):
            if not stored_refresh:
                _fail("no LINKEDIN_REFRESH_TOKEN to refresh from; run without --refresh")
            bundle = refresh_token_flow(client_id, client_secret, stored_refresh)
            console.print("[green]✔[/green] refreshed")
        else:
            bundle = authorize(client_id, client_secret)
            console.print("[green]✔[/green] authorized")
    except Exception as exc:
        _fail(f"LinkedIn auth failed: {exc}")
        return

    console.print("\n[bold]Put these in .env (and in your GitHub secrets):[/bold]\n")
    for line in bundle.env_lines():
        name, _, value = line.partition("=")
        console.print(f"  {name}=[dim]{escape(value[:6])}…{escape(value[-4:])}[/dim]"
                      if len(value) > 16 else f"  {escape(line)}")
    console.print(
        "\n[dim]Values are abbreviated here on purpose. Run with "
        "POSTPILOT_REVEAL=1 to print them in full.[/dim]"
    )
    if os.environ.get("POSTPILOT_REVEAL") == "1":
        console.print("")
        for line in bundle.env_lines():
            print(line)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
