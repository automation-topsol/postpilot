#!/usr/bin/env python3
"""Phase 0 spike — Cloudflare R2 as the public media host.

This is the check most likely to surprise us, because R2 has to satisfy a
requirement no other component does: **Meta and LinkedIn fetch our media by
URL from their own servers.** A bucket we can write to but the public cannot
read is useless, and we would only find out at publish time.

Answers:
  1. Can we authenticate and see the bucket?
  2. Can we PUT an object and HEAD it back?
  3. Can an ANONYMOUS client GET it over the custom domain? (the real test)
  4. Is the 60-day lifecycle rule actually configured?
  5. Is the public base URL a custom domain, or still r2.dev?

Run:  uv run python spike/check_r2.py
"""

from __future__ import annotations

import datetime as dt

from _common import Report, load_env, redact, require

# Written, read back, then deleted. Namespaced so it can never collide with
# real media, which always lives under {brand}/{post_id}/...
PROBE_KEY = "postpilot/_spike/access-probe.txt"

LIFECYCLE_EXPECTED_DAYS = 60


def build_client(report: Report, present: dict[str, str]):
    import boto3
    from botocore.config import Config

    # Two R2 gotchas encoded here:
    #  - region must be "auto"
    #  - boto3 >= 1.36 sends CRC32 checksum headers on every PUT by default,
    #    which R2's S3 implementation rejects. "when_required" turns that off
    #    without losing checksums where they are genuinely needed.
    config = Config(
        region_name="auto",
        signature_version="s3v4",
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 3, "mode": "standard"},
    )
    endpoint = f"https://{present['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    report.ok("R2 endpoint", endpoint, access_key=redact(present["R2_ACCESS_KEY_ID"]))
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=present["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=present["R2_SECRET_ACCESS_KEY"],
        config=config,
    )


def check_bucket(report: Report, s3, bucket: str) -> bool:
    with report.guard("bucket reachable"):
        s3.head_bucket(Bucket=bucket)
        report.ok("bucket reachable", bucket)
        return True
    return False


def check_write_and_public_read(report: Report, s3, bucket: str, public_base: str) -> None:
    """PUT → HEAD → anonymous GET → DELETE."""
    stamp = dt.datetime.now(dt.UTC).isoformat()
    body = f"postpilot access probe {stamp}\n".encode()

    from botocore.exceptions import ClientError

    wrote = False
    try:
        s3.put_object(
            Bucket=bucket,
            Key=PROBE_KEY,
            Body=body,
            ContentType="text/plain; charset=utf-8",
            # Mirrors what the real MediaStore will set, so we are testing the
            # same code path the publisher will use.
            CacheControl="public, max-age=86400",
        )
        report.ok("PUT object", PROBE_KEY, bytes=str(len(body)))
        wrote = True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"AccessDenied", "AccessDeniedException", "403"}:
            # HeadBucket succeeding while PutObject is denied is the signature
            # of a read-only API token. Naming that precisely saves the
            # operator from re-checking their keys, which are fine.
            report.fail(
                "PUT object",
                "AccessDenied — the bucket is reachable but this token cannot write. "
                "The R2 API token is read-only: in Cloudflare, create a token with "
                "'Object Read & Write' on this bucket and update R2_ACCESS_KEY_ID / "
                "R2_SECRET_ACCESS_KEY.",
            )
        else:
            report.fail("PUT object", f"{code}: {exc}")

    if not wrote:
        report.skip("HEAD object", "nothing was uploaded")
        report.skip("anonymous public GET", "nothing was uploaded — THE critical check, still unverified")
        return

    try:
        with report.guard("HEAD object"):
            head = s3.head_object(Bucket=bucket, Key=PROBE_KEY)
            report.ok(
                "HEAD object",
                "exists — this is the 'reuse instead of re-upload' path",
                size=str(head["ContentLength"]),
                etag=head.get("ETag", "?"),
                content_type=head.get("ContentType", "?"),
            )

        # --- the check that actually matters -------------------------------
        with report.guard("anonymous public GET"):
            import httpx

            url = f"{public_base.rstrip('/')}/{PROBE_KEY}"
            # No credentials at all: this is exactly what Meta's fetcher does.
            resp = httpx.get(url, timeout=20, follow_redirects=True)
            if resp.status_code == 200 and resp.content == body:
                report.ok(
                    "anonymous public GET",
                    "Meta/LinkedIn will be able to fetch our media",
                    url=url,
                    content_type=resp.headers.get("content-type", "?"),
                    cache_control=resp.headers.get("cache-control", "?"),
                )
            elif resp.status_code == 200:
                report.fail("anonymous public GET", f"200 but body mismatch ({len(resp.content)} bytes)", url=url)
            else:
                report.fail(
                    "anonymous public GET",
                    f"HTTP {resp.status_code} — bucket is not publicly readable over this domain. "
                    "Publishing WILL fail: Meta fetches media from its own servers.",
                    url=url,
                )
    finally:
        with report.guard("DELETE probe object"):
            s3.delete_object(Bucket=bucket, Key=PROBE_KEY)
            report.ok("DELETE probe object", "cleaned up")


def check_lifecycle(report: Report, s3, bucket: str) -> None:
    """The 60-day expiry rule is what keeps R2 inside the free tier."""
    from botocore.exceptions import ClientError

    try:
        conf = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchLifecycleConfiguration", "NoSuchLifecycleConfigurationError", "404"}:
            report.fail(
                "lifecycle rule",
                f"no lifecycle configuration — objects will accumulate forever. "
                f"Add a rule expiring objects after {LIFECYCLE_EXPECTED_DAYS} days.",
            )
            return
        if code in {"AccessDenied", "AccessDeniedException", "403"}:
            report.warn(
                "lifecycle rule",
                "cannot be read with this API token (bucket-level permission). "
                f"Verify by hand in the Cloudflare dashboard that {bucket} expires "
                f"objects after {LIFECYCLE_EXPECTED_DAYS} days — it is what keeps "
                "R2 inside the free tier.",
            )
            return
        raise

    rules = conf.get("Rules", [])
    enabled = [r for r in rules if r.get("Status") == "Enabled"]
    days = [r["Expiration"]["Days"] for r in enabled if "Days" in r.get("Expiration", {})]

    if not enabled:
        report.fail("lifecycle rule", f"{len(rules)} rule(s) present but none Enabled")
    elif LIFECYCLE_EXPECTED_DAYS in days:
        report.ok("lifecycle rule", f"expires objects after {LIFECYCLE_EXPECTED_DAYS} days")
    elif days:
        report.warn(
            "lifecycle rule",
            f"enabled, but expires after {days} days, not {LIFECYCLE_EXPECTED_DAYS}",
        )
    else:
        report.warn("lifecycle rule", "enabled but no day-based Expiration found")


def check_public_base(report: Report, public_base: str) -> None:
    """The r2.dev development URL is the chosen configuration — see CLAUDE.md §0.4.

    It is not a misconfiguration to flag, but it does carry a real constraint:
    Cloudflare rate-limits r2.dev and does not support it for production
    traffic. At 20-50 posts/week, with each item fetched a handful of times by
    Meta, that ceiling is nowhere near being approached — so this is recorded,
    not warned about. Moving to a custom domain is a one-line change to
    R2_PUBLIC_BASE_URL and nothing else.
    """
    if not public_base.startswith("https://"):
        report.fail("public base URL", "must be https:// — Meta will not fetch http media", url=public_base)
    elif ".r2.dev" in public_base:
        report.ok(
            "public base URL",
            "r2.dev development URL (chosen; rate-limited but ample at this volume)",
            url=public_base,
        )
    else:
        report.ok("public base URL", "custom domain", url=public_base)


def main() -> int:
    load_env()
    report = Report("Cloudflare R2 — public media host")
    report.header()

    names = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE_URL")
    present, missing = require(*names)
    if missing:
        report.missing(missing)
        return report.summary()

    bucket = present["R2_BUCKET"]
    public_base = present["R2_PUBLIC_BASE_URL"]

    check_public_base(report, public_base)

    s3 = None
    with report.guard("build R2 client"):
        s3 = build_client(report, present)
    if s3 is None:
        return report.summary()

    if check_bucket(report, s3, bucket):
        check_write_and_public_read(report, s3, bucket, public_base)
        with report.guard("lifecycle rule"):
            check_lifecycle(report, s3, bucket)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
