"""Shared helpers for the Phase 0 access spike.

THROWAWAY. Nothing in `postpilot/` may import from `spike/`. These scripts
exist to answer one question — *do we actually have the access we think we
have?* — and to record the answer before any real code is written.

Design rule for every check: a missing credential is a **SKIP**, not a crash.
The operator fills `.env` incrementally, and a half-filled `.env` must still
produce a useful report for the parts that are filled.
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load `.env` from the repo root. Real values only ever live here."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit("python-dotenv missing. Run:  uv sync")
    env_path = REPO_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def env(name: str) -> str | None:
    """Read an env var, treating empty/whitespace as absent.

    `.env.example` is copied with every key present but blank, so "" is by far
    the most common way a credential is missing.
    """
    value = os.environ.get(name, "").strip()
    return value or None


def require(*names: str) -> tuple[dict[str, str], list[str]]:
    """Split the named vars into (present, missing)."""
    present = {n: v for n in names if (v := env(n))}
    missing = [n for n in names if n not in present]
    return present, missing


def redact(value: str | None, keep: int = 4) -> str:
    """Never print a secret. Used in every line of spike output."""
    if not value:
        return "<unset>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]} ({len(value)} chars)"


class Outcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"


_STYLE = {
    Outcome.PASS: ("green", "✔"),
    Outcome.FAIL: ("red", "✘"),
    Outcome.SKIP: ("yellow", "-"),
    Outcome.WARN: ("yellow", "!"),
}


@dataclass
class Check:
    name: str
    outcome: Outcome
    detail: str = ""
    facts: dict[str, str] = field(default_factory=dict)


class Report:
    """Collects check results and prints them. Exit code reflects failures."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.checks: list[Check] = []
        try:
            from rich.console import Console

            # markup=False: a brand slug in [brackets], or a Meta error body
            # containing them, would otherwise be swallowed as rich markup.
            # highlight=False: stops rich recolouring IDs and URLs mid-line.
            self.console = Console(markup=False, highlight=False)
        except ImportError:
            self.console = None

    def _emit(self, text: str, style: str = "") -> None:
        if self.console and style:
            self.console.print(text, style=style)
        elif self.console:
            self.console.print(text)
        else:
            print(text)

    def add(self, _name: str, _outcome: Outcome, _detail: str = "", **facts: str) -> Check:
        # Leading underscores so a fact called `name`, `detail` or `outcome`
        # cannot collide with the signature — several API responses have a
        # `name` field and the collision is a TypeError at the worst moment.
        name, outcome, detail = _name, _outcome, _detail
        check = Check(name, outcome, detail, {k: str(v) for k, v in facts.items()})
        self.checks.append(check)
        style, glyph = _STYLE[outcome]
        self._emit(f"  {glyph} [{outcome.value}] {name}" + (f" — {detail}" if detail else ""), style)
        for key, value in check.facts.items():
            self._emit(f"      {key}: {value}", "dim")
        return check

    def ok(self, _name: str, _detail: str = "", **facts: str) -> Check:
        return self.add(_name, Outcome.PASS, _detail, **facts)

    def fail(self, _name: str, _detail: str = "", **facts: str) -> Check:
        return self.add(_name, Outcome.FAIL, _detail, **facts)

    def skip(self, _name: str, _detail: str = "", **facts: str) -> Check:
        return self.add(_name, Outcome.SKIP, _detail, **facts)

    def warn(self, _name: str, _detail: str = "", **facts: str) -> Check:
        return self.add(_name, Outcome.WARN, _detail, **facts)

    def missing(self, names: list[str]) -> Check:
        """Uniform SKIP for absent credentials — tells the operator what to fill."""
        return self.skip(
            "credentials",
            f"not set in .env: {', '.join(names)}",
        )

    def guard(self, name: str):
        """Context manager: any exception inside becomes a FAIL, not a crash.

        One broken check must never stop the rest of the spike from reporting.
        """
        return _Guard(self, name)

    def header(self) -> None:
        self._emit(f"\n=== {self.title} ===", "bold")

    def summary(self) -> int:
        counts = {o: sum(c.outcome is o for c in self.checks) for o in Outcome}
        parts = [f"{counts[o]} {o.value.lower()}" for o in Outcome if counts[o]]
        self._emit(f"  → {', '.join(parts) or 'nothing ran'}", "bold")
        return 1 if counts[Outcome.FAIL] else 0


class _Guard:
    def __init__(self, report: Report, name: str) -> None:
        self.report, self.name = report, name

    def __enter__(self) -> _Guard:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is None:
            return False
        detail = f"{exc_type.__name__}: {exc}"
        if os.environ.get("SPIKE_TRACEBACK"):
            traceback.print_exception(exc_type, exc, tb)
        self.report.fail(self.name, detail[:300])
        return True  # swallow: one failed check must not abort the run
