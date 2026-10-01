"""webapp/Code.js against fake Apps Script services.

What matters: rows land in the tab's own column order as plain text, only
user columns are filled, `ID` stays blank, nothing outside the brand tabs is
touched, and importing the same plan twice adds nothing the second time.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from postpilot.sheets.schema import BRANDS_HEADERS, brand_headers

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("webapp_server_harness.js")
NOW = dt.datetime(2026, 9, 25, 6, 0, tzinfo=dt.UTC)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HEADERS = list(reversed(brand_headers()))  # humans reorder columns

PLAN = """\
brand: grandinvitation

## Mon 2026-09-28 18:30
platforms: FB, IG
type: carousel
media: gold-1.png, gold-2.png
caption:
Gold is here.

---

## 2026-09-29 10:00
platforms: FB, IG
type: text
caption: FB yes, IG no

---

## 2026-09-30 12:00
platforms: FB
type: text
caption: Already typed by hand

---

## 2026-10-01 12:00
brand: notab
platforms: FB
type: text
caption: brand without a tab
"""


def run() -> dict:
    existing = {h: "" for h in HEADERS} | {
        "ID": "gi-0001", "Date": "2026-09-30", "Time": "12:00", "Platforms": "FB",
        "Type": "text", "Caption": "Already typed by hand", "Status": "scheduled",
    }
    case = {
        "now_ms": int(NOW.timestamp() * 1000),
        "plan": PLAN,
        "files": ["gold-1.png", "gold-2.png"],
        "sheets": {
            "_Brands": [
                BRANDS_HEADERS,
                ["Grand Invitation", "grandinvitation", "FB, IG", "1", "2", "", "folder-gi", "", "TRUE"],
                ["No Tab", "notab", "FB", "3", "", "", "folder-nt", "", "TRUE"],
            ],
            "grandinvitation": [HEADERS, [existing[h] for h in HEADERS]],
            "_State": [["Post ID"], ["gi-0001"]],
        },
    }
    out = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(case), capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_status_lists_brands_and_who_is_signed_in() -> None:
    status = run()["status"]
    assert status["error"] == ""
    assert status["user"] == "operator@example.com"
    assert {b["slug"] for b in status["brands"]} == {"grandinvitation", "notab"}
    assert status["template"] == "<PlanTemplate>"


def test_rows_are_appended_as_a_person_would_type_them() -> None:
    out = run()
    first = out["first"]
    assert first["rows_added"] == {"grandinvitation": 2}
    assert any("notab: brand tab is missing" in e for e in first["errors"])

    tab = out["sheets"]["grandinvitation"]
    assert len(tab["values"]) == 4  # header + hand-typed row + 2 imported
    rows = [dict(zip(HEADERS, r, strict=True)) for r in tab["values"][2:]]
    assert rows[0]["Date"] == "2026-09-28" and rows[0]["Time"] == "18:30"
    assert rows[0]["Media"] == "gold-1.png, gold-2.png"
    assert rows[0]["Type"] == "carousel"
    assert rows[1]["Caption"] == "FB yes, IG no"
    for row in rows:
        assert row["ID"] == ""
        for tool_column in ("Status", "Published URLs", "Error", "Attempts", "Last Run", "Notes", "Action"):
            assert row[tool_column] == ""
    # Plain text, so Sheets cannot turn the date into a locale date.
    assert tab["formats"] == {"3": "@"} or tab["formats"] == {"3": "@", "4": "@"}

    assert out["sheets"]["_State"]["values"] == [["Post ID"], ["gi-0001"]]  # never touched


def test_the_hand_typed_duplicate_is_not_added() -> None:
    reviews = run()["review"]["reviews"]
    assert reviews[2]["duplicate_of"] == "gi-0001"
    assert reviews[2]["importable"] is False


def test_importing_the_same_plan_twice_adds_nothing() -> None:
    out = run()
    assert out["second"]["rows_added"] == {}
    assert all("already in the Sheet" in s for s in out["second"]["skipped"] if "grandinvitation" in s)
    assert len(out["sheets"]["grandinvitation"]["values"]) == 4


def test_upload_context_gives_each_brands_folder() -> None:
    upload = run()["upload"]
    assert upload["folders"] == {"grandinvitation": "folder-gi", "notab": "folder-nt"}
    assert upload["token"] == "fake-token"


def test_the_template_shipped_with_the_webapp_matches_the_local_one() -> None:
    root = Path(__file__).parent.parent
    assert (root / "webapp" / "PlanTemplate.html").read_text() == (
        root / "postpilot" / "ui" / "plan-template.md"
    ).read_text()
