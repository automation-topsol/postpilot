#!/usr/bin/env python3
"""Phase 0 spike — Google service-account access to the Sheet and Drive.

Answers:
  1. Do the service-account credentials parse, and what is the client_email
     the operator must share things with?
  2. Can we READ the Sheet?
  3. Can we WRITE to the Sheet? (proved by creating a scratch tab, writing,
     reading back, then deleting it — never touching the operator's data)
  4. Can we read each brand's Drive folder, including Shared Drives?
  5. Does Drive give us the `md5Checksum` the deterministic R2 key depends on?

Run:  uv run python spike/check_google.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import Outcome, Report, env, load_env, redact

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SCRATCH_TAB = "_spike_probe"

# Drive MIME types we must reject: Google-native files have no real bytes to
# download, so `files.get(alt=media)` fails on them. Better to name them here
# than to let a teammate wonder why their Slides deck "doesn't work".
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."


def build_credentials(report: Report):
    """Service account only — no browser OAuth flow for Google, ever."""
    raw = env("GOOGLE_SERVICE_ACCOUNT_JSON")
    path = env("GOOGLE_SERVICE_ACCOUNT_FILE")

    if not raw and not path:
        report.missing(["GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_SERVICE_ACCOUNT_FILE)"])
        return None

    from google.oauth2.service_account import Credentials

    if raw:
        info = json.loads(raw)
        source = "GOOGLE_SERVICE_ACCOUNT_JSON"
    else:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
        source = f"file {path}"

    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    report.ok(
        "service-account credentials parsed",
        f"from {source}",
        client_email=info.get("client_email", "?"),
        project_id=info.get("project_id", "?"),
        private_key_id=redact(info.get("private_key_id")),
    )
    report.warn(
        "sharing reminder",
        "the Sheet and every Drive folder must be shared with the client_email above "
        "— service accounts cannot own Drive files",
    )
    return creds


def check_sheet(report: Report, creds) -> list[dict]:
    """Read the Sheet, prove we can write, and return the _Brands rows."""
    sheet_id = env("SHEET_ID")
    if not sheet_id:
        report.missing(["SHEET_ID"])
        return []

    import gspread

    client = gspread.authorize(creds)

    with report.guard("open Sheet by ID"):
        book = client.open_by_key(sheet_id)
        tabs = [w.title for w in book.worksheets()]
        report.ok(
            "Sheet readable",
            book.title,
            sheet_id=sheet_id,
            tabs=", ".join(tabs) or "(none)",
        )

    if not any(c.outcome is Outcome.PASS and c.name == "Sheet readable" for c in report.checks):
        return []

    # --- write probe -------------------------------------------------------
    # A scratch tab proves write access without risking a single cell of the
    # operator's content. It is deleted again even if the read-back fails.
    scratch = None
    with report.guard("Sheet writable"):
        try:
            scratch = book.add_worksheet(title=SCRATCH_TAB, rows=2, cols=2)
            scratch.update(values=[["postpilot", "spike"]], range_name="A1:B1")
            echoed = scratch.get("A1:B1")
            if echoed and echoed[0] == ["postpilot", "spike"]:
                report.ok("Sheet writable", "scratch tab written and read back")
            else:
                report.fail("Sheet writable", f"read-back mismatch: {echoed!r}")
        finally:
            if scratch is not None:
                book.del_worksheet(scratch)

    # --- _Brands -----------------------------------------------------------
    if "_Brands" not in tabs:
        report.warn(
            "_Brands tab",
            "not present yet — `postpilot sheet init` will create it in Phase 1",
        )
        return []

    with report.guard("read _Brands"):
        rows = book.worksheet("_Brands").get_all_records()
        active = [r for r in rows if str(r.get("Active", "")).strip().lower() in {"true", "yes", "1"}]
        report.ok(
            "_Brands readable",
            f"{len(rows)} row(s), {len(active)} active",
            slugs=", ".join(str(r.get("Slug", "?")) for r in rows) or "(none)",
        )
        return rows
    return []


def check_drive(report: Report, creds, brands: list[dict], extra: list[tuple[str, str]] | None = None) -> None:
    """Read each brand's Drive folder and confirm md5Checksum is available."""
    extra = extra or []
    from googleapiclient.discovery import build

    drive = build("drive", "v3", credentials=creds, cache_discovery=False)

    folders: list[tuple[str, str]] = [
        (str(b.get("Slug") or "?"), str(b.get("Drive Folder ID")).strip())
        for b in brands
        if str(b.get("Drive Folder ID", "")).strip()
    ]
    # Before _Brands is populated, folders can be supplied directly:
    #   DRIVE_FOLDERS=slug=id,slug=id      (or a single DRIVE_FOLDER_ID)
    #   --folder slug=id --folder slug=id
    folders.extend(extra)
    if not folders and (override := env("DRIVE_FOLDER_ID")):
        folders = [("(DRIVE_FOLDER_ID)", override)]

    if not folders:
        report.skip("Drive folders", "no Drive Folder IDs in _Brands and DRIVE_FOLDER_ID unset")
        return

    for slug, folder_id in folders:
        with report.guard(f"Drive folder [{slug}]"):
            # supportsAllDrives / includeItemsFromAllDrives are what make this
            # work when the folder lives on a Shared Drive rather than My Drive.
            resp = (
                drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="files(id, name, mimeType, size, md5Checksum)",
                    pageSize=25,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files = resp.get("files", [])
            native = [f for f in files if f.get("mimeType", "").startswith(GOOGLE_NATIVE_PREFIX)]
            binary = [f for f in files if f not in native]
            no_md5 = [f["name"] for f in binary if not f.get("md5Checksum")]

            report.ok(
                f"Drive folder [{slug}] readable",
                f"{len(files)} file(s): {len(binary)} usable, {len(native)} Google-native",
                folder_id=folder_id,
                sample=", ".join(f["name"] for f in binary[:5]) or "(empty)",
            )
            if native:
                report.warn(
                    f"Drive folder [{slug}] Google-native files",
                    f"rejected at validation: {', '.join(f['name'] for f in native[:5])}",
                )
            if no_md5:
                # The deterministic R2 key is {…}-{drive_md5}.{ext}. No md5, no key.
                report.fail(
                    f"Drive folder [{slug}] md5Checksum",
                    f"missing on: {', '.join(no_md5[:5])} — R2 keys depend on it",
                )
            elif binary:
                report.ok(f"Drive folder [{slug}] md5Checksum", "present on all usable files")


def parse_folders(values: list[str]) -> list[tuple[str, str]]:
    """Parse `slug=folder_id` pairs from --folder flags or DRIVE_FOLDERS."""
    pairs = []
    for raw in values:
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            slug, _, folder_id = item.partition("=")
            pairs.append((slug.strip(), folder_id.strip()) if folder_id else ("(unnamed)", slug.strip()))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 Google access spike")
    parser.add_argument(
        "--folder",
        action="append",
        default=[],
        metavar="SLUG=ID",
        help="test a Drive folder before _Brands is populated; repeatable",
    )
    args = parser.parse_args()

    load_env()
    report = Report("Google — service account, Sheets, Drive")
    report.header()

    creds = None
    with report.guard("build credentials"):
        creds = build_credentials(report)

    if creds is None:
        report.skip("Sheets + Drive checks", "no usable credentials")
        return report.summary()

    extra = parse_folders(args.folder or [])
    if not extra and (from_env := env("DRIVE_FOLDERS")):
        extra = parse_folders([from_env])

    brands = check_sheet(report, creds)
    check_drive(report, creds, brands, extra)
    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
