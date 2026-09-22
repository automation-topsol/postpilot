"""Google Drive: resolve the Media column to real files, and fetch bytes.

The `Media` cell holds file *names* typed by a human, so resolution is where
most media problems surface. The rule that matters: a name matching more than
one file is an **error**, never a guess. Publishing the wrong picture cannot be
undone, and v1 has no post deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from postpilot.logging import get_logger

log = get_logger(__name__)

# Google-native files (Docs, Slides, Sheets) have no real bytes behind them, so
# `files.get(alt=media)` cannot download one. Rejected with a clear message
# rather than an opaque API error.
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

# A Drive file ID is long and opaque; a file name almost never is. Used to tell
# "the user pasted a link" from "the user typed a name".
_ID_MIN_LENGTH = 20


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int
    md5: str

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith("image/")

    @property
    def is_video(self) -> bool:
        return self.mime_type.startswith("video/")

    @property
    def extension(self) -> str:
        _, _, ext = self.name.rpartition(".")
        return ext.lower() if ext and ext != self.name else ""

    @property
    def fingerprint(self) -> str:
        """What the content hash uses — identity plus content, not the name.

        Renaming a file in Drive must not re-open a published post; changing
        its bytes must.
        """
        return f"{self.id}:{self.md5}"


@dataclass
class Resolution:
    """The outcome of turning one Media cell into files, in order."""

    files: list[DriveFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def fingerprint(self) -> str:
        return "|".join(f.fingerprint for f in self.files)


class DriveClient:
    """Reads a brand's folder once per run and answers from that snapshot."""

    def __init__(self, credentials) -> None:
        from googleapiclient.discovery import build

        self._service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._folders: dict[str, list[DriveFile]] = {}

    # -- listing ------------------------------------------------------------
    def list_folder(self, folder_id: str, *, refresh: bool = False) -> list[DriveFile]:
        if not refresh and folder_id in self._folders:
            return self._folders[folder_id]

        files: list[DriveFile] = []
        page_token: str | None = None
        while True:
            # supportsAllDrives / includeItemsFromAllDrives are what make this
            # work when the folder lives on a Shared Drive rather than My Drive.
            response = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, size, md5Checksum)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for item in response.get("files", []):
                files.append(
                    DriveFile(
                        id=item["id"],
                        name=item.get("name", ""),
                        mime_type=item.get("mimeType", ""),
                        size=int(item.get("size", 0) or 0),
                        md5=item.get("md5Checksum", "") or "",
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        self._folders[folder_id] = files
        log.debug("listed Drive folder %s: %d file(s)", folder_id, len(files))
        return files

    # -- resolution ---------------------------------------------------------
    def resolve(self, folder_id: str, entries: list[str]) -> Resolution:
        """Turn Media-cell entries into files, preserving carousel order."""
        resolution = Resolution()
        if not entries:
            return resolution

        try:
            available = self.list_folder(folder_id)
        except Exception as exc:  # broad on purpose: any Drive failure belongs in the Error column
            resolution.errors.append(f"cannot read the brand's Drive folder: {exc}")
            return resolution

        by_id = {f.id: f for f in available}
        by_name: dict[str, list[DriveFile]] = {}
        for item in available:
            by_name.setdefault(item.name.strip().casefold(), []).append(item)

        for entry in entries:
            found = self._resolve_one(entry, by_id, by_name, resolution)
            if found is not None:
                resolution.files.append(found)

        return resolution

    def _resolve_one(
        self,
        entry: str,
        by_id: dict[str, DriveFile],
        by_name: dict[str, list[DriveFile]],
        resolution: Resolution,
    ) -> DriveFile | None:
        # A Drive link was already reduced to its ID during parsing.
        if len(entry) >= _ID_MIN_LENGTH and entry in by_id:
            return self._check(by_id[entry], resolution)

        matches = by_name.get(entry.strip().casefold(), [])

        if not matches:
            # A bare name that also looks like an ID but is not in this folder
            # is usually a link to a file somewhere else — say so specifically.
            if len(entry) >= _ID_MIN_LENGTH and " " not in entry:
                resolution.errors.append(
                    f"{entry!r} is not in this brand's Drive folder (a link to a file elsewhere?)"
                )
            else:
                resolution.errors.append(f"no file named {entry!r} in the brand's Drive folder")
            return None

        if len(matches) > 1:
            # Never guess. Two files with the same name is a human problem
            # with a human fix: rename one of them.
            resolution.errors.append(
                f"ambiguous: {len(matches)} files named {entry!r} in the folder — rename one"
            )
            return None

        return self._check(matches[0], resolution)

    def _check(self, item: DriveFile, resolution: Resolution) -> DriveFile | None:
        if item.mime_type.startswith(GOOGLE_NATIVE_PREFIX):
            resolution.errors.append(
                f"{item.name!r} is a Google {item.mime_type.rsplit('.', 1)[-1]} — "
                "only real image and video files can be posted"
            )
            return None
        if not (item.is_image or item.is_video):
            resolution.errors.append(f"{item.name!r} is not an image or a video ({item.mime_type})")
            return None
        if not item.md5:
            # Without an md5 the deterministic R2 key cannot be built, so the
            # file would be re-uploaded on every single run.
            resolution.errors.append(f"{item.name!r} has no checksum in Drive — re-upload it")
            return None
        return item

    # -- bytes --------------------------------------------------------------
    def download(self, file_id: str) -> bytes:
        data = self._service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        log.debug("downloaded %s (%d bytes)", file_id, len(data))
        return data
