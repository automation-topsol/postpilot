"""An in-memory stand-in for the Sheet.

Behaves like `SheetClient` as far as `sync` is concerned, and records every
write so tests can assert on what *would* reach Google. No network, ever.
"""

from __future__ import annotations

from typing import Any

from postpilot.models import LogEntry
from postpilot.sheets.client import Tab, TabWriter
from postpilot.sheets.schema import a1_column


class FakeSheetClient:
    def __init__(self, tabs: dict[str, tuple[list[str], list[list[str]]]]) -> None:
        self._tabs: dict[str, Tab] = {
            title: Tab(title=title, headers=headers, rows=[list(r) for r in rows], sheet_id=i)
            for i, (title, (headers, rows)) in enumerate(tabs.items())
        }
        self.flushed: list[tuple[str, list[tuple[str, Any]]]] = []
        self.replaced: dict[str, list[list[str]]] = {}
        self.appended_logs: list[LogEntry] = []
        self.title = "Fake Sheet"

    # -- reads --------------------------------------------------------------
    def read(self, title: str, *, refresh: bool = False) -> Tab | None:
        return self._tabs.get(title)

    def tab_titles(self) -> list[str]:
        return list(self._tabs)

    # -- writes -------------------------------------------------------------
    def writer(self, tab: Tab) -> TabWriter:
        return TabWriter(tab=tab)

    def flush(self, writer: TabWriter) -> int:
        if writer.is_empty:
            return 0
        recorded = [(w.a1, w.values) for w in writer.writes]
        self.flushed.append((writer.tab.title, recorded))
        # Apply the writes so a second sync in the same test sees them, which
        # is what makes "ID is assigned once and never changes" testable.
        tab = self._tabs[writer.tab.title]
        for a1, values in recorded:
            self._apply(tab, a1, values)
        writer.writes.clear()
        return len(recorded)

    def replace_rows(self, title: str, headers: list[str], rows: list[list[Any]]) -> None:
        self.replaced[title] = [list(r) for r in rows]
        self._tabs[title] = Tab(
            title=title,
            headers=headers,
            rows=[[str(c) for c in r] for r in rows],
            sheet_id=self._tabs[title].sheet_id if title in self._tabs else 99,
        )

    def append_log(self, entries: list[LogEntry]) -> None:
        self.appended_logs.extend(entries)

    # -- helpers ------------------------------------------------------------
    def _apply(self, tab: Tab, a1: str, values: list[list[Any]]) -> None:
        start = a1.split(":")[0]
        col_letters = "".join(c for c in start if c.isalpha())
        row_number = int("".join(c for c in start if c.isdigit()))
        col_index = 0
        for char in col_letters:
            col_index = col_index * 26 + (ord(char) - 64)
        col_index -= 1

        index = row_number - 2
        while len(tab.rows) <= index:
            tab.rows.append([""] * len(tab.headers))
        row = tab.rows[index]
        while len(row) < len(tab.headers):
            row.append("")
        for offset, value in enumerate(values[0]):
            if col_index + offset < len(row):
                row[col_index + offset] = str(value)

    def cell(self, title: str, row_number: int, column: str) -> str:
        tab = self._tabs[title]
        index = tab.headers.index(column)
        row = tab.rows[row_number - 2]
        return row[index] if index < len(row) else ""

    def written_ranges(self, title: str) -> list[str]:
        return [a1 for tab_title, writes in self.flushed if tab_title == title for a1, _ in writes]


def state_column(client: FakeSheetClient, post_id: str, platform: str, column: str) -> str:
    """Read one field out of the rewritten `_State` tab."""
    from postpilot.sheets.schema import STATE_HEADERS

    index = STATE_HEADERS.index(column)
    for row in client.replaced.get("_State", []):
        if row[0] == post_id and row[2] == platform:
            return row[index]
    return ""


def a1(col_index: int, row_number: int) -> str:
    return f"{a1_column(col_index)}{row_number}"
