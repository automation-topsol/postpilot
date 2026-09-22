"""`postpilot` — the command line surface.

Commands that are not built yet exit with a clear "phase N" message rather
than a traceback or, worse, a silent success.
"""

from __future__ import annotations

import typer
from rich.console import Console

from postpilot import __version__
from postpilot.config import ENV_PATH, REPO_ROOT, MissingSetting, Settings, write_default_config
from postpilot.logging import configure
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
    console.print(f"[bold red]✘[/bold red] {message}")
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
        console.print(f"[yellow]![/yellow] {note}")
    for problem in report.problems:
        console.print(f"[red]✘[/red] {problem}")
    if report.problems:
        raise typer.Exit(code=1)


@app.command()
def sync(
    brand: str = typer.Option(None, "--brand", help="Only sync this brand."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute everything, write nothing."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Validate rows, assign IDs, compute hashes, reconcile `_State`."""
    settings = _settings(verbose)
    try:
        client = _client(settings)
        result = run_sync(
            client,
            tz_name=settings.tunables.timezone,
            only_brand=brand,
            write=not dry_run,
        )
    except MissingSetting as exc:
        _fail(str(exc))
        return

    if not dry_run and result.log_entries:
        client.append_log(result.log_entries)

    for problem in result.problems:
        console.print(f"[red]✘[/red] {problem}")
    for warning in result.warnings:
        console.print(f"[yellow]![/yellow] {warning}")

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
        result = run_sync(client, tz_name=settings.tunables.timezone, only_brand=brand, write=False)
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
def prepare(post: str = typer.Option(None, "--post")) -> None:
    """Download, normalise and upload media for due posts. (Phase 2)"""
    _not_yet("prepare", 2)


@app.command()
def publish(
    dry_run: bool = typer.Option(False, "--dry-run"),
    live: bool = typer.Option(False, "--live"),
    confirm: bool = typer.Option(False, "--confirm"),
    brand: str = typer.Option(None, "--brand"),
    post: str = typer.Option(None, "--post"),
) -> None:
    """Publish due posts. Local live runs require --live AND --confirm. (Phase 3)"""
    if live and not confirm:
        _fail("--live also requires --confirm. Nothing was sent.")
    _not_yet("publish", 3)


@app.command()
def summary() -> None:
    """Send the daily digest to Telegram, or to `_Log` if unconfigured. (Phase 7)"""
    _not_yet("summary", 7)


@app.command()
def doctor() -> None:
    """Check every credential and external dependency. (Phase 2)"""
    console.print("[yellow]![/yellow] `doctor` arrives in Phase 2. See CLAUDE.md §6.")
    console.print("[dim]Until then: uv run python spike/run_all.py[/dim]")
    raise typer.Exit(code=2)


auth_app = typer.Typer(no_args_is_help=True, help="Guided token acquisition.")
app.add_typer(auth_app, name="auth")


@auth_app.command("meta")
def auth_meta(brand: str = typer.Option(..., "--brand")) -> None:
    """Exchange a short-lived user token for long-lived Page tokens. (Phase 4)"""
    console.print("[yellow]![/yellow] `auth meta` arrives in Phase 4.")
    console.print("[dim]Working prototype: uv run python spike/meta_exchange_token.py --help[/dim]")
    raise typer.Exit(code=2)


@auth_app.command("linkedin")
def auth_linkedin() -> None:
    """Acquire and refresh the single LinkedIn token. (Phase 6)

    Takes no --brand: one token covers every Company Page. See CLAUDE.md §0.1.
    """
    _not_yet("auth linkedin", 6)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
