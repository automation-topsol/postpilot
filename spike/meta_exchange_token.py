#!/usr/bin/env python3
"""Phase 0 helper — turn a short-lived user token into long-lived Page tokens.

This is the fiddliest part of Meta setup and the one that silently produces an
unusable scheduler, so it is worth automating even in the spike. It is also the
working prototype of `postpilot auth meta` (Phase 4).

What it does:
  1. Inspects whatever token is in META_PAGE_TOKEN_<SLUG> (or --token).
  2. If it is a short-lived USER token, exchanges it for a long-lived one via
     grant_type=fb_exchange_token.
  3. Calls /me/accounts with that, listing every Page you administer together
     with its linked Instagram Business account.
  4. Prints the exact .env line for each Page — REDACTED by default, so a
     secret never lands in a terminal transcript by accident.

Page tokens derived from a long-lived user token do not expire, which is the
only thing that makes an unattended scheduler viable.

Run:
    uv run python spike/meta_exchange_token.py --token <short_lived_user_token>
    uv run python spike/meta_exchange_token.py --brand grandinvitation
    uv run python spike/meta_exchange_token.py --brand grandinvitation --write-env
"""

from __future__ import annotations

import argparse
import datetime as dt
import re

import httpx
from _common import REPO_ROOT, Report, env, load_env, redact

GRAPH_API_VERSION = "v25.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def slug_to_env(slug: str) -> str:
    return "META_PAGE_TOKEN_" + slug.upper().replace("-", "_")


def env_slug(name: str) -> str:
    return name.removeprefix("META_PAGE_TOKEN_").lower().replace("_", "-")


def describe(report: Report, token: str, app_id: str, app_secret: str) -> dict:
    data = httpx.get(
        f"{GRAPH}/debug_token",
        params={"input_token": token, "access_token": f"{app_id}|{app_secret}"},
        timeout=30,
    ).json()
    info = data.get("data", {})
    if not info.get("is_valid"):
        report.fail("token", str(info.get("error", {}).get("message", "invalid token")))
        return {}

    expires_at = info.get("expires_at", 0)
    kind = str(info.get("type", "?"))
    if expires_at:
        left = dt.datetime.fromtimestamp(expires_at, dt.UTC) - dt.datetime.now(dt.UTC)
        report.ok("token", f"{kind}, expires in {left.days}d {int(left.seconds / 3600)}h")
    else:
        report.ok("token", f"{kind}, never expires")
    return info


def exchange(report: Report, token: str, app_id: str, app_secret: str) -> str | None:
    """Short-lived user token -> long-lived (~60 day) user token."""
    resp = httpx.get(
        f"{GRAPH}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        err = resp.json().get("error", {})
        report.fail("exchange for long-lived user token", str(err.get("message", resp.text[:200])))
        return None
    body = resp.json()
    long_lived = body["access_token"]
    days = int(body.get("expires_in", 0)) // 86400
    report.ok(
        "exchange for long-lived user token",
        f"valid ~{days} days" if days else "issued",
        value=redact(long_lived),
    )
    return long_lived


def list_pages(report: Report, user_token: str) -> list[dict]:
    """Every Page the user administers, with its Page token and IG link."""
    resp = httpx.get(
        f"{GRAPH}/me/accounts",
        params={
            "fields": "id,name,access_token,tasks,instagram_business_account{id,username}",
            "limit": 100,
            "access_token": user_token,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        err = resp.json().get("error", {})
        report.fail("list Pages", str(err.get("message", resp.text[:200])))
        return []
    pages = resp.json().get("data", [])
    if not pages:
        report.fail(
            "list Pages",
            "no Pages returned — the user administers none, or the token lacks "
            "pages_show_list",
        )
    else:
        report.ok("list Pages", f"{len(pages)} Page(s)")
    return pages


def write_env(report: Report, assignments: dict[str, str]) -> None:
    """Update .env in place, replacing existing keys and appending new ones.

    Deliberately preserves everything else byte-for-byte: .env is hand-edited
    and full of comments the operator wrote.
    """
    path = REPO_ROOT / ".env"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()

    for key, value in assignments.items():
        pattern = re.compile(rf"^{re.escape(key)}=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{key}={value}"
                report.ok(f"{key}", "updated in .env", value=redact(value))
                break
        else:
            lines.append(f"{key}={value}")
            report.ok(f"{key}", "appended to .env", value=redact(value))

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token", help="short-lived USER token from the Graph API Explorer")
    parser.add_argument("--brand", help="read the starting token from META_PAGE_TOKEN_<SLUG> instead")
    parser.add_argument(
        "--write-env",
        action="store_true",
        help="write each Page token straight into .env (never printed in full)",
    )
    parser.add_argument("--reveal", action="store_true", help="print tokens in full (careful)")
    args = parser.parse_args()

    load_env()
    report = Report("Meta — exchange for long-lived Page tokens")
    report.header()

    app_id, app_secret = env("META_APP_ID"), env("META_APP_SECRET")
    if not (app_id and app_secret):
        report.missing(["META_APP_ID", "META_APP_SECRET"])
        return report.summary()

    token = args.token or (env(slug_to_env(args.brand)) if args.brand else None)
    if not token:
        report.fail("input token", "pass --token <user_token> or --brand <slug>")
        return report.summary()

    info = describe(report, token, app_id, app_secret)
    if not info:
        return report.summary()

    kind = str(info.get("type", "")).upper()
    if kind == "PAGE":
        report.warn(
            "already a Page token",
            "nothing to exchange. If it still expires, the USER token it came "
            "from was short-lived — redo the exchange from that one.",
        )
        return report.summary()

    user_token = token if not info.get("expires_at") else exchange(report, token, app_id, app_secret)
    if not user_token:
        return report.summary()

    pages = list_pages(report, user_token)
    assignments: dict[str, str] = {}
    for page in pages:
        ig = page.get("instagram_business_account") or {}
        slug = re.sub(r"[^a-z0-9]+", "", page.get("name", "").lower()) or page["id"]
        key = slug_to_env(slug)
        report.ok(
            page.get("name", "?"),
            f"page_id {page['id']}"
            + (f" · IG @{ig.get('username')} ({ig['id']})" if ig else " · no IG linked"),
            env_var=key,
            page_token=page["access_token"] if args.reveal else redact(page["access_token"]),
        )
        assignments[key] = page["access_token"]

    if args.write_env and assignments:
        write_env(report, assignments)
    elif assignments:
        report.warn(
            "not written",
            "re-run with --write-env to put these into .env, or --reveal to print them",
        )

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
