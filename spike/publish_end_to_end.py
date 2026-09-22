#!/usr/bin/env python3
"""Phase 0 — the whole chain, for real: Drive -> normalise -> R2 -> publish.

Read-only checks prove *access*. This proves the *publish path*, which is the
honest end of Phase 0. It is also the working prototype of Phases 2, 4 and 5,
so the shapes here (policies, deterministic keys, container polling) are the
shapes the real code should take.

    Drive metadata (id, md5Checksum)
      -> deterministic R2 key  {brand}/{post_id}/{platform}/{policy}-{md5}.{ext}
      -> HEAD: exists? reuse : (download -> normalise -> PUT)
      -> publish

Defaults to --dry-run. Publishing for real needs --live --confirm, exactly as
the real CLI will, because v1 cannot delete a published post.

Run:
    uv run python spike/publish_end_to_end.py --brand grandinvitation
    uv run python spike/publish_end_to_end.py --brand grandinvitation --live --confirm
"""

from __future__ import annotations

import argparse
import io
import time

import httpx
from _common import Report, env, load_env

GRAPH_API_VERSION = "v25.0"
GRAPH = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Part of every R2 key. Bump when a normalisation rule changes so previously
# transformed files are never silently reused. See docs/MEDIA_POLICIES.md.
POLICY_VERSION = "v1"

# Verified limits — docs/MEDIA_POLICIES.md §2.1 and §3.1.
IG_MAX_BYTES = 8 * 1024 * 1024
IG_MAX_WIDTH = 1440
IG_ASPECT_MIN = 4 / 5       # 0.80 - taller than this must be padded
IG_ASPECT_MAX = 1.91        # wider than this must be padded
FB_MAX_BYTES = 10 * 1024 * 1024
FB_MAX_EDGE = 2048

# Crop rather than pad when the overshoot is this small: padding a barely
# out-of-range image wastes more of the frame than trimming it.
CROP_TOLERANCE = 0.05

IG_POLL_SECONDS = 5
IG_TIMEOUT_SECONDS = 300


# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------
def drive_client(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def build_creds():
    import json

    from google.oauth2.service_account import Credentials

    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    return Credentials.from_service_account_info(
        json.loads(raw),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )


def find_media(report: Report, drive, folder_id: str, wanted: str | None) -> dict | None:
    """Locate one image in the brand's folder.

    A name matching more than one file is an error, never a guess — the real
    validator does the same. Silently picking one would publish the wrong
    picture, which v1 cannot undo.
    """
    resp = (
        drive.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id, name, mimeType, size, md5Checksum)",
            pageSize=100,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = [f for f in resp.get("files", []) if f.get("mimeType", "").startswith("image/")]

    if wanted:
        matches = [f for f in files if f["name"] == wanted]
        if len(matches) > 1:
            report.fail("find media", f"ambiguous: {len(matches)} files named {wanted!r}")
            return None
        if not matches:
            report.fail("find media", f"no image named {wanted!r} in the folder")
            return None
        chosen = matches[0]
    elif not files:
        report.fail("find media", "no images in the folder")
        return None
    else:
        chosen = files[0]
        if len(files) > 1:
            report.warn("find media", f"{len(files)} images present; using the first: {chosen['name']}")

    if not chosen.get("md5Checksum"):
        report.fail("find media", "no md5Checksum — the deterministic R2 key depends on it")
        return None

    report.ok(
        "find media",
        chosen["name"],
        drive_id=chosen["id"],
        mime=chosen["mimeType"],
        size=f"{int(chosen.get('size', 0)) / 1024:.0f} KB",
        md5=chosen["md5Checksum"],
    )
    return chosen


def download(report: Report, drive, file_id: str) -> bytes:
    data = drive.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    report.ok("download from Drive", f"{len(data) / 1024:.0f} KB")
    return data


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
def _flatten(img):
    """Composite onto white: JPEG has no alpha, and the source here is a PNG."""
    from PIL import Image

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        canvas = Image.new("RGB", img.size, (255, 255, 255))
        canvas.paste(img, mask=img.split()[-1])
        return canvas
    return img.convert("RGB")


def _encode(img, max_bytes: int) -> bytes:
    """Re-encode at descending quality until it fits."""
    for quality in (92, 88, 84, 80, 75, 70, 65):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        if buf.tell() <= max_bytes:
            return buf.getvalue()
    return buf.getvalue()


def normalise_instagram(report: Report, data: bytes) -> bytes:
    """JPEG, sRGB, aspect within 4:5-1.91:1, long edge <= 1440, <= 8 MB."""
    from PIL import Image, ImageFilter

    img = _flatten(Image.open(io.BytesIO(data)))
    w, h = img.size
    aspect = w / h
    note = f"{w}x{h} aspect {aspect:.3f}"

    target = min(max(aspect, IG_ASPECT_MIN), IG_ASPECT_MAX)
    if abs(target - aspect) > 1e-6:
        overshoot = abs(target - aspect) / aspect
        if overshoot <= CROP_TOLERANCE:
            # Barely out of range: trim rather than pad.
            if aspect > target:
                new_w = round(h * target)
                img = img.crop(((w - new_w) // 2, 0, (w - new_w) // 2 + new_w, h))
            else:
                new_h = round(w / target)
                img = img.crop((0, (h - new_h) // 2, w, (h - new_h) // 2 + new_h))
            note += f" -> cropped to {target:.3f} (within {CROP_TOLERANCE:.0%})"
        else:
            # Pad onto a blurred copy of itself: keeps the whole subject
            # visible, which cropping a portrait poster would not.
            if aspect > target:
                canvas_w, canvas_h = w, round(w / target)
            else:
                canvas_w, canvas_h = round(h * target), h
            bg = img.resize((canvas_w, canvas_h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(40))
            bg.paste(img, ((canvas_w - w) // 2, (canvas_h - h) // 2))
            img = bg
            note += f" -> padded (blurred) to {canvas_w}x{canvas_h}"

    if max(img.size) > IG_MAX_WIDTH:
        img.thumbnail((IG_MAX_WIDTH, IG_MAX_WIDTH), Image.LANCZOS)
        note += f" -> resized to {img.size[0]}x{img.size[1]}"

    out = _encode(img, IG_MAX_BYTES)
    report.ok("normalise for Instagram", note, result=f"JPEG {len(out) / 1024:.0f} KB {img.size[0]}x{img.size[1]}")
    return out


def normalise_facebook(report: Report, data: bytes) -> bytes:
    """JPEG, sRGB, <= 10 MB. No aspect constraint: Facebook accepts any ratio."""
    from PIL import Image

    img = _flatten(Image.open(io.BytesIO(data)))
    note = f"{img.size[0]}x{img.size[1]}"
    if max(img.size) > FB_MAX_EDGE:
        img.thumbnail((FB_MAX_EDGE, FB_MAX_EDGE), Image.LANCZOS)
        note += f" -> resized to {img.size[0]}x{img.size[1]}"
    out = _encode(img, FB_MAX_BYTES)
    report.ok("normalise for Facebook", note, result=f"JPEG {len(out) / 1024:.0f} KB")
    return out


# --------------------------------------------------------------------------
# R2
# --------------------------------------------------------------------------
def r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        config=Config(
            region_name="auto",
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def r2_key(brand: str, post_id: str, platform: str, md5: str) -> str:
    return f"{brand}/{post_id}/{platform}/{POLICY_VERSION}-{md5}.jpg"


def upload(report: Report, s3, bucket: str, key: str, data: bytes) -> str:
    """HEAD first: an identical key means identical bytes, so reuse it."""
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        report.ok("R2 reuse", "object already present — no re-upload", key=key)
    except ClientError:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType="image/jpeg",
            CacheControl="public, max-age=86400",
        )
        report.ok("R2 upload", f"{len(data) / 1024:.0f} KB", key=key)

    url = f"{env('R2_PUBLIC_BASE_URL').rstrip('/')}/{key}"
    probe = httpx.head(url, timeout=20, follow_redirects=True)
    if probe.status_code == 200:
        report.ok("R2 public URL", "publicly fetchable", url=url)
    else:
        report.fail("R2 public URL", f"HTTP {probe.status_code} — Meta will not be able to fetch it", url=url)
    return url


# --------------------------------------------------------------------------
# Publish
# --------------------------------------------------------------------------
def publish_facebook(report: Report, token: str, page_id: str, url: str, caption: str) -> None:
    resp = httpx.post(
        f"{GRAPH}/{page_id}/photos",
        data={"url": url, "caption": caption, "access_token": token},
        timeout=90,
    )
    if resp.status_code != 200:
        report.fail("Facebook publish", f"HTTP {resp.status_code}: {resp.text[:300]}")
        return
    body = resp.json()
    # post_id is the feed post; id is only the photo object.
    post_id = body.get("post_id", "")
    report.ok(
        "Facebook publish",
        "PUBLISHED",
        photo_id=str(body.get("id", "?")),
        post_id=str(post_id),
        permalink=f"https://facebook.com/{post_id}" if post_id else "?",
    )


def publish_instagram(report: Report, token: str, ig_id: str, url: str, caption: str) -> None:
    created = httpx.post(
        f"{GRAPH}/{ig_id}/media",
        data={"image_url": url, "caption": caption, "access_token": token},
        timeout=90,
    )
    if created.status_code != 200:
        report.fail("Instagram container", f"HTTP {created.status_code}: {created.text[:300]}")
        return
    container = created.json()["id"]
    report.ok("Instagram container", "created", container_id=container)

    deadline = time.monotonic() + IG_TIMEOUT_SECONDS
    status = "?"
    while time.monotonic() < deadline:
        probe = httpx.get(
            f"{GRAPH}/{container}",
            params={"fields": "status_code,status", "access_token": token},
            timeout=30,
        ).json()
        status = probe.get("status_code", "?")
        if status in {"FINISHED", "ERROR", "EXPIRED"}:
            break
        time.sleep(IG_POLL_SECONDS)

    if status != "FINISHED":
        # In production this is `unknown`, with the container ID recorded so
        # reconciliation can resolve it. Never a blind retry.
        report.fail("Instagram container ready", f"status={status} (production: unknown)", container_id=container)
        return
    report.ok("Instagram container ready", "FINISHED")

    published = httpx.post(
        f"{GRAPH}/{ig_id}/media_publish",
        data={"creation_id": container, "access_token": token},
        timeout=90,
    )
    if published.status_code != 200:
        report.fail("Instagram publish", f"HTTP {published.status_code}: {published.text[:300]}")
        return
    media_id = published.json()["id"]
    link = httpx.get(
        f"{GRAPH}/{media_id}", params={"fields": "permalink", "access_token": token}, timeout=30
    ).json()
    report.ok("Instagram publish", "PUBLISHED", media_id=media_id, permalink=str(link.get("permalink", "?")))


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 end-to-end publish test")
    parser.add_argument("--brand", required=True)
    parser.add_argument("--folder", help="Drive folder ID (default: from DRIVE_FOLDERS)")
    parser.add_argument("--file", help="exact file name in the folder")
    parser.add_argument("--post-id", default="spike-0001")
    parser.add_argument("--caption", required=True, help="used where no override is given")
    # Mirrors the Sheet's `Caption (Facebook)` / `Caption (Instagram)` columns.
    parser.add_argument("--caption-fb", help="overrides --caption for Facebook")
    parser.add_argument("--caption-ig", help="overrides --caption for Instagram")
    # IG ignores links entirely (captions are not clickable), so a Link only
    # ever makes sense appended to the FB caption.
    parser.add_argument("--link", help="appended to the Facebook caption; IG ignores it")
    parser.add_argument("--platform", choices=["fb", "ig", "both"], default="both")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    load_env()
    live = args.live and args.confirm
    report = Report(f"End-to-end publish [{args.brand}] {'LIVE' if live else 'DRY RUN'}")
    report.header()

    if args.live and not args.confirm:
        report.fail("--live", "requires --confirm as well; nothing was sent")
        return report.summary()

    folder = args.folder
    if not folder:
        for pair in (env("DRIVE_FOLDERS") or "").split(","):
            slug, _, fid = pair.strip().partition("=")
            if slug == args.brand:
                folder = fid
    if not folder:
        report.fail("Drive folder", f"no folder for {args.brand}; pass --folder")
        return report.summary()

    token = env("META_PAGE_TOKEN_" + args.brand.upper().replace("-", "_"))
    if not token:
        report.missing(["META_PAGE_TOKEN_" + args.brand.upper().replace("-", "_")])
        return report.summary()

    creds = build_creds()
    drive = drive_client(creds)

    meta = find_media(report, drive, folder, args.file)
    if not meta:
        return report.summary()

    raw = download(report, drive, meta["id"])
    md5 = meta["md5Checksum"]

    s3, bucket = r2_client(), env("R2_BUCKET")
    page = httpx.get(
        f"{GRAPH}/me", params={"fields": "id,name", "access_token": token}, timeout=30
    ).json()
    page_id = page.get("id", "")
    ig = httpx.get(
        f"{GRAPH}/{page_id}",
        params={"fields": "instagram_business_account{id,username}", "access_token": token},
        timeout=30,
    ).json().get("instagram_business_account") or {}
    report.ok("target", page.get("name", "?"), page_id=page_id, instagram=ig.get("username", "none"))

    captions = {
        "facebook": args.caption_fb or args.caption,
        "instagram": args.caption_ig or args.caption,
    }
    if args.link:
        captions["facebook"] = f"{captions['facebook']}\n\n{args.link}"
    for platform, text in captions.items():
        report.ok(
            f"caption [{platform}]",
            f"{len(text)} chars"
            + (" (override)" if (args.caption_fb if platform == "facebook" else args.caption_ig) else ""),
            text=text.replace("\n", " / "),
        )

    targets = []
    if args.platform in {"fb", "both"}:
        targets.append(("facebook", normalise_facebook))
    if args.platform in {"ig", "both"} and ig:
        targets.append(("instagram", normalise_instagram))

    urls: dict[str, str] = {}
    for platform, normalise in targets:
        data = normalise(report, raw)
        key = r2_key(args.brand, args.post_id, platform, md5)
        urls[platform] = upload(report, s3, bucket, key, data)

    if not live:
        report.skip(
            "publish",
            "DRY RUN — media is prepared and publicly reachable, but nothing was sent. "
            "Re-run with --live --confirm to publish.",
        )
        return report.summary()

    report.warn("LIVE", "publishing now — this cannot be undone by the tool")
    if "facebook" in urls:
        with report.guard("Facebook publish"):
            publish_facebook(report, token, page_id, urls["facebook"], captions["facebook"])
    if "instagram" in urls:
        with report.guard("Instagram publish"):
            publish_instagram(report, token, ig["id"], urls["instagram"], captions["instagram"])

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
