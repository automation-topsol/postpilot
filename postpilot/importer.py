"""Import a reviewed plan: files to Drive, rows to the Sheet, then `sync`.

The importer writes exactly what a teammate would type — the user-filled
columns only, with `ID` blank — and then runs the ordinary `sync`, which
assigns IDs and validates as it always does. It never touches `_State`
directly, never leases, never publishes. So the delivery guarantee does not
depend on this module at all; the worst it can do is add a wrong row, which is
exactly the risk a human typing already carries.

Drive rules, so `Media` names always resolve to exactly one file:

- a file whose bytes (md5) are already in the folder is **reused**, not
  uploaded again — re-importing a plan costs nothing;
- a new file whose name is already taken is uploaded as `name-<md5[:6]>.ext`,
  because two files with one name is the "ambiguous" error `sync` refuses.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import mimetypes
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from postpilot.drive import DriveFile
from postpilot.logging import get_logger
from postpilot.models import Brand
from postpilot.plan import PlannedPost, Review, existing_key
from postpilot.sheets.client import SheetClient
from postpilot.sheets.parse import parse_brands, parse_post
from postpilot.sheets.schema import BRANDS_TAB

log = get_logger(__name__)


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Reading what already exists
# --------------------------------------------------------------------------
def load_brands(client: SheetClient) -> dict[str, Brand]:
    tab = client.read(BRANDS_TAB)
    if tab is None:
        return {}
    brands, _, _ = parse_brands(tab.headers, tab.rows)
    return {b.slug: b for b in brands if b.active}


def existing_posts(client: SheetClient, brands: dict[str, Brand], tz_name: str) -> dict[tuple, str]:
    """Duplicate keys for every row already in the brand tabs -> its label."""
    keys: dict[tuple, str] = {}
    for brand in brands.values():
        tab = client.read(brand.slug)
        if tab is None:
            continue
        for index, row in enumerate(tab.rows):
            post = parse_post(brand, tab.headers, row, tab.row_number(index), tz_name)
            if post is None or post.scheduled_at is None:
                continue
            keys[existing_key(post)] = post.post_id or f"row {tab.row_number(index)}"
    return keys


# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------
class DriveUploader:
    """Uploads as the signed-in person; lists each folder once."""

    def __init__(self, credentials, service=None) -> None:
        if service is None:
            from googleapiclient.discovery import build

            service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._service = service
        self._folders: dict[str, list[DriveFile]] = {}

    def list_folder(self, folder_id: str) -> list[DriveFile]:
        if folder_id not in self._folders:
            files: list[DriveFile] = []
            token = None
            while True:
                response = (
                    self._service.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields="nextPageToken, files(id, name, mimeType, size, md5Checksum)",
                        pageSize=1000,
                        pageToken=token,
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                files.extend(
                    DriveFile(
                        id=f["id"], name=f.get("name", ""), mime_type=f.get("mimeType", ""),
                        size=int(f.get("size", 0) or 0), md5=f.get("md5Checksum", "") or "",
                    )
                    for f in response.get("files", [])
                )
                token = response.get("nextPageToken")
                if not token:
                    break
            self._folders[folder_id] = files
        return self._folders[folder_id]

    def ensure(self, folder_id: str, local: Path) -> tuple[str, bool]:
        """Make `local` present in the folder. Returns (name to use, uploaded?)."""
        md5 = md5_of(local)
        files = self.list_folder(folder_id)
        names = Counter(f.name.strip().casefold() for f in files)

        for existing in files:
            # Same bytes, and a name that resolves to exactly this file.
            if existing.md5 == md5 and names[existing.name.strip().casefold()] == 1:
                return existing.name, False

        name = local.name
        if names[name.casefold()]:
            name = f"{local.stem}-{md5[:6]}{local.suffix}"
            if names[name.casefold()]:  # vanishingly unlikely; never guess
                raise RuntimeError(f"cannot find a free name for {local.name!r} in the Drive folder")

        from googleapiclient.http import MediaFileUpload

        mime = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        body = {"name": name, "parents": [folder_id]}
        media = MediaFileUpload(str(local), mimetype=mime, resumable=True)
        created = (
            self._service.files()
            .create(body=body, media_body=media, fields="id, name, mimeType, size, md5Checksum",
                    supportsAllDrives=True)
            .execute()
        )
        files.append(
            DriveFile(
                id=created["id"], name=created["name"], mime_type=created.get("mimeType", mime),
                size=int(created.get("size", 0) or 0), md5=created.get("md5Checksum", md5),
            )
        )
        log.info("uploaded %s to Drive as %s", local.name, name)
        return name, True


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------
@dataclass
class ImportResult:
    rows_added: dict[str, int] = field(default_factory=dict)  # brand -> count
    uploaded: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def import_reviews(
    reviews: list[Review],
    *,
    files_dir: Path,
    brands: dict[str, Brand],
    client: SheetClient,
    uploader: DriveUploader,
) -> ImportResult:
    """Upload and append everything importable. One append per brand tab."""
    result = ImportResult()
    rows: dict[str, list[PlannedPost]] = {}

    for review in reviews:
        post = review.post
        label = f"{post.brand} {post.date} {post.time}".strip()
        if not post.include:
            result.skipped.append(f"{label}: removed in the preview")
            continue
        if not review.importable:
            reason = f"already in the Sheet ({review.duplicate_of})" if review.duplicate_of else "has errors"
            result.skipped.append(f"{label}: {reason}")
            continue

        brand = brands[post.brand]
        try:
            renamed = []
            for name in post.media:
                drive_name, uploaded = uploader.ensure(brand.drive_folder_id, files_dir / name)
                (result.uploaded if uploaded else result.reused).append(drive_name)
                renamed.append(drive_name)
        except Exception as exc:
            result.errors.append(f"{label}: Drive upload failed — {exc}")
            continue

        post.media = renamed
        rows.setdefault(post.brand, []).append(post)

    for slug, posts in rows.items():
        tab = client.read(slug, refresh=True)
        if tab is None:
            result.errors.append(f"{slug}: brand tab is missing — run `postpilot sheet init`")
            continue
        try:
            client.append_rows(slug, [p.row_for(tab.headers) for p in posts])
        except Exception as exc:
            result.errors.append(f"{slug}: could not add rows to the Sheet — {exc}")
            continue
        result.rows_added[slug] = len(posts)

    # Keep the order stable and each name once, for a readable summary.
    result.uploaded = list(dict.fromkeys(result.uploaded))
    result.reused = list(dict.fromkeys(result.reused))
    return result


def batch_id(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    return now.strftime("%Y%m%d-%H%M%S")
