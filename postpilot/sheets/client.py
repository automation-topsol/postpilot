"""Batched gspread access.

Sheets API quotas are per-minute and unforgiving, and this runs every 15
minutes against several tabs. The rule the whole module exists to enforce:
**one read per tab per run, one batch update per tab per run.** Nothing here
may read or write a cell at a time inside a loop.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import gspread

from postpilot.logging import get_logger
from postpilot.models import LogEntry
from postpilot.sheets.schema import (
    ACTION_CHOICES,
    ACTIVE_CHOICES,
    BRANDS_HEADERS,
    COL_ACTION,
    COL_TOOL_END,
    COL_TOOL_START,
    COL_TYPE,
    LOG_HEADERS,
    LOG_TAB,
    MAX_DATA_ROWS,
    RESERVED_TABS,
    STATE_HEADERS,
    TYPE_CHOICES,
    a1_column,
    brand_headers,
)

log = get_logger(__name__)


@dataclass
class Tab:
    """One tab, read once into memory."""

    title: str
    headers: list[str]
    rows: list[list[str]]  # data rows only, header excluded
    sheet_id: int

    def row_number(self, index: int) -> int:
        """Data index -> 1-based sheet row (header occupies row 1)."""
        return index + 2


@dataclass
class PendingWrite:
    """A single-cell or range update, accumulated and flushed in one call."""

    a1: str
    values: list[list[Any]]


@dataclass
class TabWriter:
    """Collects updates for one tab, then flushes them as a single batch."""

    tab: Tab
    writes: list[PendingWrite] = field(default_factory=list)

    def set_cell(self, row_number: int, col_index: int, value: Any) -> None:
        self.writes.append(PendingWrite(f"{a1_column(col_index)}{row_number}", [[value]]))

    def set_range(self, row_number: int, col_start: int, values: list[Any]) -> None:
        """Contiguous columns in one row — far cheaper than one write each."""
        if not values:
            return
        end = col_start + len(values) - 1
        a1 = f"{a1_column(col_start)}{row_number}:{a1_column(end)}{row_number}"
        self.writes.append(PendingWrite(a1, [list(values)]))

    @property
    def is_empty(self) -> bool:
        return not self.writes


class SheetClient:
    """Opens the spreadsheet once and serves every tab from that handle."""

    def __init__(self, credentials, sheet_id: str) -> None:
        self._client = gspread.authorize(credentials)
        self._sheet_id = sheet_id
        self._book: gspread.Spreadsheet | None = None
        self._tabs: dict[str, Tab] = {}

    # -- opening ------------------------------------------------------------
    @property
    def book(self) -> gspread.Spreadsheet:
        if self._book is None:
            self._book = self._client.open_by_key(self._sheet_id)
            log.debug("opened spreadsheet %s", self._book.title)
        return self._book

    @property
    def title(self) -> str:
        return self.book.title

    def tab_titles(self) -> list[str]:
        return [w.title for w in self.book.worksheets()]

    def brand_tab_titles(self) -> list[str]:
        return [t for t in self.tab_titles() if t not in RESERVED_TABS]

    # -- reading ------------------------------------------------------------
    def read(self, title: str, *, refresh: bool = False) -> Tab | None:
        """Read a whole tab in ONE call, and cache it for the rest of the run."""
        if not refresh and title in self._tabs:
            return self._tabs[title]
        try:
            worksheet = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            return None

        values = worksheet.get_all_values()
        headers = [h.strip() for h in values[0]] if values else []
        rows = values[1:] if len(values) > 1 else []
        tab = Tab(title=title, headers=headers, rows=rows, sheet_id=worksheet.id)
        self._tabs[title] = tab
        log.debug("read tab %s: %d data row(s)", title, len(rows))
        return tab

    # -- writing ------------------------------------------------------------
    def writer(self, tab: Tab) -> TabWriter:
        return TabWriter(tab=tab)

    def flush(self, writer: TabWriter) -> int:
        """Send every accumulated update for one tab as a single batch."""
        if writer.is_empty:
            return 0
        worksheet = self.book.worksheet(writer.tab.title)
        payload = [{"range": w.a1, "values": w.values} for w in writer.writes]
        worksheet.batch_update(payload, value_input_option="RAW")
        log.info("wrote %d range(s) to %s", len(payload), writer.tab.title)
        count = len(payload)
        writer.writes.clear()
        return count

    def replace_rows(self, title: str, headers: list[str], rows: list[list[Any]]) -> None:
        """Rewrite a whole machine-owned tab (`_State`) in one update.

        Only ever used on tabs no human edits. Clearing first would leave the
        tab momentarily empty, so the write covers the old extent too and pads
        with blanks — one call, no window where the state is missing.
        """
        worksheet = self.book.worksheet(title)
        width = len(headers)
        previous = len(self.read(title).rows) if self.read(title) else 0
        padded = [list(r) + [""] * (width - len(r)) for r in rows]
        blanks = [[""] * width for _ in range(max(0, previous - len(rows)))]
        body = [headers, *padded, *blanks]
        end = f"{a1_column(width - 1)}{len(body)}"
        worksheet.update(values=body, range_name=f"A1:{end}", value_input_option="RAW")
        log.info("rewrote %s: %d row(s)", title, len(rows))
        self._tabs.pop(title, None)

    def append_log(self, entries: list[LogEntry]) -> None:
        """Append to `_Log` in one call. Append-only, never rewritten."""
        if not entries:
            return
        rows = [
            [
                e.timestamp.astimezone(dt.UTC).isoformat(timespec="seconds"),
                e.brand_slug,
                e.post_id,
                e.platform.value if e.platform else "",
                e.action,
                e.result,
                e.details[:500],
            ]
            for e in entries
        ]
        self.book.worksheet(LOG_TAB).append_rows(rows, value_input_option="RAW")
        log.info("appended %d row(s) to %s", len(rows), LOG_TAB)
        self._tabs.pop(LOG_TAB, None)

    # -- structure (sheet init) --------------------------------------------
    def ensure_tab(self, title: str, headers: list[str], *, rows: int = MAX_DATA_ROWS) -> tuple[Any, bool]:
        """Create the tab if absent; repair its header row if it drifted."""
        created = False
        try:
            worksheet = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            worksheet = self.book.add_worksheet(title=title, rows=rows, cols=max(len(headers), 12))
            created = True

        existing = worksheet.row_values(1) if not created else []
        if [h.strip() for h in existing] != headers:
            end = f"{a1_column(len(headers) - 1)}1"
            worksheet.update(values=[headers], range_name=f"A1:{end}", value_input_option="RAW")
        self._tabs.pop(title, None)
        return worksheet, created

    def apply_structure(self, requests: list[dict]) -> None:
        """Send freeze/validation/protection/hide requests in one API call."""
        if requests:
            self.book.batch_update({"requests": requests})
            log.info("applied %d structural change(s)", len(requests))


# --------------------------------------------------------------------------
# Structural requests (Sheets API v4 shapes)
# --------------------------------------------------------------------------
def freeze_header(sheet_id: int) -> dict:
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    }


def set_hidden(sheet_id: int, hidden: bool) -> dict:
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "hidden": hidden},
            "fields": "hidden",
        }
    }


def bold_header(sheet_id: int, width: int) -> dict:
    return {
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "endColumnIndex": width},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }
    }


def dropdown(sheet_id: int, col_index: int, choices: list[str], *, strict: bool = True) -> dict:
    """One-of-list validation on a whole column below the header."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": MAX_DATA_ROWS + 1,
                "startColumnIndex": col_index,
                "endColumnIndex": col_index + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": c} for c in choices if c],
                },
                "showCustomUi": True,
                # Non-strict so a blank cell is always allowed: `Action` is
                # blank by default and must stay effortless to clear.
                "strict": strict,
            },
        }
    }


def protect_range(sheet_id: int, start_col: int, end_col: int, description: str) -> dict:
    """Warn-only protection on the tool-owned columns.

    Deliberately `warningOnly`. Hard protection is enforced by editor list, and
    getting that wrong with a service account can lock the Sheet's own owner out
    of their columns. The risk here is an accidental paste, which a warning
    already prevents, so the gentler control is the right trade.
    """
    return {
        "addProtectedRange": {
            "protectedRange": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "startColumnIndex": start_col,
                    "endColumnIndex": end_col,
                },
                "description": description,
                "warningOnly": True,
            }
        }
    }


def brand_tab_requests(sheet_id: int) -> list[dict]:
    """Everything a brand tab needs: freeze, bold, dropdowns, protection."""
    return [
        freeze_header(sheet_id),
        bold_header(sheet_id, len(brand_headers())),
        dropdown(sheet_id, COL_TYPE, TYPE_CHOICES),
        dropdown(sheet_id, COL_ACTION, ACTION_CHOICES, strict=False),
        protect_range(sheet_id, COL_TOOL_START, COL_TOOL_END, "PostPilot writes these — edit via Action instead"),
    ]


def brands_tab_requests(sheet_id: int) -> list[dict]:
    return [
        freeze_header(sheet_id),
        bold_header(sheet_id, len(BRANDS_HEADERS)),
        dropdown(sheet_id, BRANDS_HEADERS.index("Active"), ACTIVE_CHOICES, strict=False),
    ]


def state_tab_requests(sheet_id: int) -> list[dict]:
    """`_State` is machine-owned and hidden — it is the real source of truth."""
    return [
        freeze_header(sheet_id),
        bold_header(sheet_id, len(STATE_HEADERS)),
        protect_range(sheet_id, 0, len(STATE_HEADERS), "PostPilot state — do not edit"),
        set_hidden(sheet_id, True),
    ]


def log_tab_requests(sheet_id: int) -> list[dict]:
    return [
        freeze_header(sheet_id),
        bold_header(sheet_id, len(LOG_HEADERS)),
        protect_range(sheet_id, 0, len(LOG_HEADERS), "PostPilot log — append only"),
    ]
