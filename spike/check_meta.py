#!/usr/bin/env python3
"""Phase 0 spike — Facebook Pages + Instagram Business publishing access.

Read-only by default. It proves we *could* publish without actually doing it:
token validity, scopes, page reachability, the IG account link, and the IG
publishing quota. Add `--publish` to send one real image post.

Answers:
  1. Is the Page token valid, and when does it expire?
  2. Does it carry the scopes FB and IG publishing need?
  3. Is the Page reachable, and does it have the publishing tasks we need?
  4. Is an Instagram Business account linked, and is its ID what the Sheet says?
  5. How much of the IG 24-hour publishing quota is left?
  6. (--publish) Does an end-to-end image post actually work?

Run:
    uv run python spike/check_meta.py --brand <slug>
    uv run python spike/check_meta.py --brand <slug> --publish --image-url https://...
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

from _common import Report, env, load_env, redact

# Pinned deliberately, and in one place only — see docs/MEDIA_POLICIES.md §1.
# Phase 1 moves this constant into postpilot/apis.py; the spike must use the
# same version so what we prove here is what we ship.
GRAPH_API_VERSION = "v25.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# What each platform needs. Checked against the token's actual scopes so a
# missing permission is named now, not at 3am on a failed publish.
FB_SCOPES = {"pages_show_list", "pages_read_engagement", "pages_manage_posts"}
IG_SCOPES = {"instagram_basic", "instagram_content_publish"}

IG_CONTAINER_POLL_SECONDS = 5
IG_CONTAINER_TIMEOUT_SECONDS = 300  # matches the 5-min timeout in the brief


def slug_to_env(slug: str) -> str:
    return "META_PAGE_TOKEN_" + slug.upper().replace("-", "_")


def get(report: Report, path: str, token: str, **params) -> dict | None:
    """GET the Graph API, reporting the error body rather than raising.

    Meta's error bodies are the single most useful diagnostic here, and they
    are lost if we let httpx raise, so they are surfaced verbatim.
    """
    import httpx

    params["access_token"] = token
    try:
        resp = httpx.get(f"{GRAPH}/{path.lstrip('/')}", params=params, timeout=30)
    except httpx.HTTPError as exc:
        report.fail(f"GET /{path}", f"transport error: {exc}")
        return None

    if resp.status_code == 200:
        return resp.json()

    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    err = body.get("error", {})
    report.fail(
        f"GET /{path}",
        f"HTTP {resp.status_code}: {err.get('message', resp.text[:200])}",
        type=str(err.get("type", "?")),
        code=str(err.get("code", "?")),
        subcode=str(err.get("error_subcode", "-")),
    )
    return None


def check_token(report: Report, token: str) -> dict:
    """Inspect the token with /debug_token — expiry, scopes, type."""
    app_id, app_secret = env("META_APP_ID"), env("META_APP_SECRET")
    if not (app_id and app_secret):
        report.skip(
            "token introspection",
            "META_APP_ID / META_APP_SECRET unset — cannot call /debug_token, "
            "so expiry and scopes are unknown",
        )
        return {}

    data = get(report, "debug_token", f"{app_id}|{app_secret}", input_token=token)
    if not data:
        return {}

    info = data.get("data", {})
    if not info.get("is_valid"):
        report.fail("token valid", str(info.get("error", {}).get("message", "token is not valid")))
        return info

    expires_at = info.get("expires_at", 0)
    if expires_at == 0:
        # Long-lived Page tokens minted from a long-lived user token do not
        # expire. That is exactly what we want for an unattended scheduler.
        report.ok("token valid", "never expires (long-lived Page token)", type=str(info.get("type", "?")))
    else:
        expiry = dt.datetime.fromtimestamp(expires_at, dt.UTC)
        days = (expiry - dt.datetime.now(dt.UTC)).days
        detail = f"expires {expiry:%Y-%m-%d} ({days} days)"
        (report.warn if days < 14 else report.ok)("token valid", detail, type=str(info.get("type", "?")))

    scopes = set(info.get("scopes", []))
    for label, needed in (("Facebook", FB_SCOPES), ("Instagram", IG_SCOPES)):
        gap = needed - scopes
        if gap:
            report.fail(f"{label} scopes", f"missing: {', '.join(sorted(gap))}")
        else:
            report.ok(f"{label} scopes", ", ".join(sorted(needed)))
    return info


def check_page(report: Report, token: str) -> dict:
    """Confirm the token's Page and the tasks it grants."""
    page = get(report, "me", token, fields="id,name,tasks,link")
    if not page:
        return {}

    tasks = page.get("tasks", [])
    report.ok(
        "Page reachable",
        page.get("name", "?"),
        page_id=str(page.get("id", "?")),
        link=str(page.get("link", "?")),
        tasks=", ".join(tasks) or "(none reported)",
    )
    # CREATE_CONTENT is the task the /photos edge actually requires.
    if tasks and "CREATE_CONTENT" not in tasks:
        report.fail("Page CREATE_CONTENT task", f"token holder has {tasks} — cannot publish")
    elif tasks:
        report.ok("Page CREATE_CONTENT task", "present")
    return page


def check_instagram(report: Report, token: str, page_id: str, expected_ig_id: str | None) -> str | None:
    """Find the linked IG Business account and check its publishing quota."""
    data = get(report, page_id, token, fields="instagram_business_account{id,username,name}")
    if data is None:
        return None

    ig = data.get("instagram_business_account")
    if not ig:
        report.warn(
            "Instagram Business account",
            "no IG account linked to this Page — IG publishing unavailable for this brand. "
            "That is fine if the brand's Enabled Platforms omit IG.",
        )
        return None

    ig_id = str(ig["id"])
    report.ok(
        "Instagram Business account",
        f"@{ig.get('username', '?')}",
        ig_user_id=ig_id,
        name=str(ig.get("name", "?")),
    )
    if expected_ig_id and expected_ig_id != ig_id:
        # A silent mismatch here would publish to the wrong account.
        report.fail(
            "Instagram User ID matches Sheet",
            f"_Brands says {expected_ig_id}, Page is linked to {ig_id}",
        )
    elif expected_ig_id:
        report.ok("Instagram User ID matches Sheet", ig_id)

    quota = get(report, f"{ig_id}/content_publishing_limit", token, fields="config,quota_usage")
    if quota and quota.get("data"):
        entry = quota["data"][0]
        used = entry.get("quota_usage", 0)
        cap = entry.get("config", {}).get("quota_total", "?")
        report.ok(
            "IG publishing quota",
            f"{used} of {cap} used in the last 24h",
            note="publish must check this BEFORE every IG attempt",
        )
    return ig_id


def publish_fb_image(report: Report, token: str, page_id: str, image_url: str, caption: str) -> None:
    import httpx

    with report.guard("LIVE Facebook image post"):
        resp = httpx.post(
            f"{GRAPH}/{page_id}/photos",
            data={"url": image_url, "caption": caption, "access_token": token},
            timeout=60,
        )
        body = resp.json()
        if resp.status_code != 200:
            report.fail("LIVE Facebook image post", f"HTTP {resp.status_code}: {body}")
            return
        # post_id is the feed post; id is only the photo object. The state
        # machine records post_id as Remote ID.
        report.ok(
            "LIVE Facebook image post",
            "published",
            photo_id=str(body.get("id", "?")),
            post_id=str(body.get("post_id", "?")),
            url=f"https://facebook.com/{body.get('post_id', '')}",
        )


def publish_ig_image(report: Report, token: str, ig_id: str, image_url: str, caption: str) -> None:
    """The two-step container flow — create, poll, publish."""
    import httpx

    with report.guard("LIVE Instagram image post"):
        created = httpx.post(
            f"{GRAPH}/{ig_id}/media",
            data={"image_url": image_url, "caption": caption, "access_token": token},
            timeout=60,
        )
        if created.status_code != 200:
            report.fail("LIVE Instagram container", f"HTTP {created.status_code}: {created.json()}")
            return
        container_id = created.json()["id"]
        report.ok("LIVE Instagram container", "created", container_id=container_id)

        # Poll until FINISHED. A timeout here is the classic `unknown` case:
        # the container exists, so its ID must be recorded for reconciliation.
        deadline = time.monotonic() + IG_CONTAINER_TIMEOUT_SECONDS
        status = "?"
        while time.monotonic() < deadline:
            probe = httpx.get(
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
                timeout=30,
            ).json()
            status = probe.get("status_code", "?")
            if status in {"FINISHED", "ERROR", "EXPIRED"}:
                break
            time.sleep(IG_CONTAINER_POLL_SECONDS)

        if status != "FINISHED":
            report.fail(
                "LIVE Instagram container ready",
                f"status_code={status} — in production this is `unknown`, "
                f"with container_id stored for reconciliation",
                container_id=container_id,
            )
            return
        report.ok("LIVE Instagram container ready", "FINISHED")

        published = httpx.post(
            f"{GRAPH}/{ig_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=60,
        )
        if published.status_code != 200:
            report.fail("LIVE Instagram publish", f"HTTP {published.status_code}: {published.json()}")
            return
        media_id = published.json()["id"]
        permalink = httpx.get(
            f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token}, timeout=30
        ).json()
        report.ok(
            "LIVE Instagram publish",
            "published",
            media_id=media_id,
            permalink=str(permalink.get("permalink", "?")),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 Meta access spike")
    parser.add_argument("--brand", required=True, help="brand slug, e.g. acme-realty")
    parser.add_argument("--ig-id", help="expected Instagram User ID from _Brands, to cross-check")
    parser.add_argument("--publish", action="store_true", help="send ONE real post (not a drill)")
    parser.add_argument("--image-url", help="publicly reachable JPEG URL for --publish")
    parser.add_argument("--caption", default="PostPilot Phase 0 access spike — please ignore.")
    parser.add_argument("--platform", choices=["fb", "ig", "both"], default="both")
    args = parser.parse_args()

    load_env()
    report = Report(f"Meta — Facebook + Instagram [{args.brand}]")
    report.header()

    var = slug_to_env(args.brand)
    token = env(var)
    if not token:
        report.missing([var])
        return report.summary()
    report.ok("Page token found", var, value=redact(token))

    check_token(report, token)
    page = check_page(report, token)
    page_id = str(page.get("id", "")) if page else ""
    if not page_id:
        report.skip("Instagram checks", "Page unreachable")
        return report.summary()

    ig_id = check_instagram(report, token, page_id, args.ig_id)

    if args.publish:
        if not args.image_url:
            report.fail("--publish", "--image-url is required and must be publicly reachable")
        else:
            report.warn("LIVE MODE", "about to publish real content to a real audience")
            if args.platform in {"fb", "both"}:
                publish_fb_image(report, token, page_id, args.image_url, args.caption)
            if args.platform in {"ig", "both"} and ig_id:
                publish_ig_image(report, token, ig_id, args.image_url, args.caption)
    else:
        report.skip("live publish", "read-only run; pass --publish --image-url ... to post for real")

    return report.summary()


if __name__ == "__main__":
    sys.exit(main())
