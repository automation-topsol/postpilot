#!/usr/bin/env python3
"""Phase 0 spike — run every access check and print one verdict.

Each check runs in its own subprocess so that a hard crash in one (a missing
system library, an SDK blowing up on import) cannot hide the results of the
others. LinkedIn is deliberately absent: API access is pending, see CLAUDE.md §0.2.

Run:  uv run python spike/run_all.py [--brand <slug>]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from _common import Report, load_env

SPIKE_DIR = Path(__file__).resolve().parent

# ffmpeg/ffprobe are preinstalled on ubuntu-latest; locally they are the one
# thing a developer must install by hand. `doctor` will check the same pair.
REQUIRED_BINARIES = ("ffmpeg", "ffprobe")


def check_toolchain(report: Report) -> None:
    for binary in REQUIRED_BINARIES:
        path = shutil.which(binary)
        if path:
            version = subprocess.run(
                [binary, "-version"], capture_output=True, text=True, check=False
            ).stdout.splitlines()
            report.ok(binary, version[0][:60] if version else "present", path=path)
        else:
            report.fail(binary, "not on PATH — media normalisation will fail (brew install ffmpeg)")


def run_script(name: str, args: list[str]) -> int:
    # Flush before handing stdout to the child, or the parent's buffered
    # separators land after the child's unbuffered output and the report
    # reads out of order.
    print(f"\n{'─' * 70}", flush=True)
    result = subprocess.run(
        [sys.executable, str(SPIKE_DIR / name), *args],
        cwd=SPIKE_DIR,
        check=False,
    )
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run every Phase 0 access check")
    parser.add_argument("--brand", help="brand slug for the Meta check; defaults to every META_PAGE_TOKEN_* found")
    args = parser.parse_args()

    load_env()

    report = Report("Local toolchain")
    report.header()
    check_toolchain(report)
    failures = report.summary()

    failures += run_script("check_google.py", [])
    failures += run_script("check_r2.py", [])
    # Optional by design — an unconfigured notifier SKIPs rather than fails.
    failures += run_script("check_telegram.py", [])

    brands = [args.brand] if args.brand else discover_brands()
    if not brands:
        print(f"\n{'─' * 70}", flush=True)
        meta = Report("Meta — Facebook + Instagram")
        meta.header()
        meta.skip("Meta checks", "no META_PAGE_TOKEN_<SLUG> found in .env and no --brand given")
        meta.summary()
    for slug in brands:
        failures += run_script("check_meta.py", ["--brand", slug])

    print(f"\n{'═' * 70}", flush=True)
    if failures:
        print(f"PHASE 0: {failures} check group(s) reported failures — see above.")
    else:
        print("PHASE 0: no failures. (SKIPs mean a credential is not in .env yet.)")
    print("LinkedIn is not checked here — API access pending (CLAUDE.md §0.2).")
    return 1 if failures else 0


def discover_brands() -> list[str]:
    """Derive brand slugs from whichever META_PAGE_TOKEN_* vars exist."""
    import os

    prefix = "META_PAGE_TOKEN_"
    slugs = []
    for key, value in os.environ.items():
        if key.startswith(prefix) and value.strip():
            slugs.append(key[len(prefix) :].lower().replace("_", "-"))
    return sorted(slugs)


if __name__ == "__main__":
    sys.exit(main())
