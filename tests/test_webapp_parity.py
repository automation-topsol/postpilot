"""The hosted importer (webapp/Logic.js) must judge plans exactly as Python does.

`webapp/Logic.js` is a port of `plan.py` + the row parsing in `sheets/parse.py`.
A preview that says "ready" for a post `sync` will reject — or the reverse —
is the failure this file exists to catch. Each case runs through both and every
field the preview shows is compared.

No Google: Node runs the JS on the same in-memory Sheet data Python gets.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from postpilot.plan import existing_key, parse_plan, review_plan
from postpilot.sheets.parse import parse_brands, parse_post
from postpilot.sheets.schema import BRANDS_HEADERS, brand_headers
from tests.test_importer import PLAN as IMPORTER_PLAN

NODE = shutil.which("node")
HARNESS = Path(__file__).with_name("webapp_harness.js")
TZ = "Asia/Karachi"
NOW = dt.datetime(2026, 9, 25, 6, 0, tzinfo=dt.UTC)

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

BRAND_ROWS = [
    ["Grand Invitation", "grandinvitation", "FB, IG", "111", "222", "", "folder-gi", "#wedding #invites", "TRUE"],
    ["Restockly POS", "restocklypos", "LI", "", "", "", "folder-rp", "", "yes"],
    ["Fb Only", "fbonly", "FB", "333", "", "", "folder-fb", "", "TRUE"],
    ["Blank Active", "blankactive", "FB", "555", "", "", "folder-ba", "", ""],  # blank = inactive
    ["Retired", "retired", "FB", "444", "", "", "folder-x", "", "FALSE"],
    ["", "", "", "", "", "", "", "", ""],
]

FILES = ["gold-1.png", "gold-2.png", "gold-3.JPG", "promo.mp4", "clip.MOV", "notes.pdf", "a b.png"]


def tab(rows: list[dict[str, str]], headers: list[str] | None = None) -> dict:
    headers = headers or brand_headers()
    return {"headers": headers, "rows": [[r.get(h, "") for h in headers] for r in rows]}


# Rows already in the Sheet, in the many shapes humans type dates and times.
TABS = {
    "grandinvitation": tab([
        {"ID": "gi-0001", "Date": "28/09/2026", "Time": "18:30", "Platforms": "FB", "Type": "text",
         "Caption": "Already   scheduled\nhere."},
        {"ID": "gi-0002", "Date": "Oct 2, 2026", "Time": "6:30 PM", "Platforms": "FB", "Type": "text",
         "Caption": "October text"},
        {"Date": "2 October 2026", "Time": "9 AM", "Platforms": "FB", "Type": "text", "Caption": "No id yet"},
        {"Date": "03/04/2026", "Time": "10:00", "Platforms": "FB", "Type": "text", "Caption": "Ambiguous"},
        {"Date": "", "Caption": "draft"},
        {},
    ], headers=list(reversed(brand_headers()))),  # humans reorder columns
    "fbonly": tab([
        {"ID": "fo-0001", "Date": "2026/10/05", "Time": "12:00:00", "Platforms": "FB", "Type": "text",
         "Caption": "Hi"},
    ]),
}

EDGE_PLAN = """\r
brand: grandinvitation\r
\r
## Mon 2026-09-28 18:30\r
platforms: FB\r
type: text\r
caption:\r
Already scheduled here.\r
\r
---\r
## Tue 2026-09-28 6 PM\r
platforms: FB, IG, TW\r
type: carousel\r
media: gold-1.png\r
link: grandinvitation.com\r
---\r
""" + """
## 2026-09-29 6PM
platforms: fb ig
type: Carousel
media: gold-1.png, gold-2.png, gold-3.JPG, promo.mp4
caption: Same line caption
caption ig: has #own tag

---

## 2026-09-29 18.30
platforms: IG
type: reel
media: gold-1.png
caption:
Reel with an image

---

## 2026-09-30 6:30pm
brand: restocklypos
platforms: LI
type: text
caption:
LinkedIn while access is pending

---

## 2026-09-30 25:00
platforms: FB
type: text
caption: bad time

---

## 2026-13-01 10:00
platforms: FB
type: text
caption: bad date

---

## 2026-02-30 10:00
platforms: FB
type: text
caption: impossible date

---

## Wed 2026-10-01 10:00
platforms: FB, IG
type: text

---

## 2026-10-01 11:00
platforms: FB, IG
type: image
media: notes.pdf
caption:\u00a0Non\u00a0breaking

---

## 2026-10-01 12:00
platforms: IG
type: image
media: a b.png

---

## 2026-10-01 13:00
platforms: FB
type: image
media: missing.png
link: https://ok.example
caption:
Missing file

---

## 2026-09-20 10:00
platforms: FB
type: text
caption: in the past

---

## 2026-10-02 18:30
platforms: FB
type: text
caption:
October    TEXT

---

## 2026-10-02 9:00 am
platforms: FB
type: text
caption: No id yet

---

## 2026-10-03 10:00
platforms: FB
type: text
caption: twin

---

## 2026-10-03 10:00
platforms: FB
type: text
caption:   twin

---

## 2026-10-04 10:00
brand: nobody
platforms: FB
type: text
caption: unknown brand

---

## Just a heading with no date
platforms: FB
type: text
caption: undated

---

## 2026-10-05 12:00
brand: fbonly
platforms: FB, LI
type: video
caption: Hi

---

## 2026-10-05 12:00
brand: retired
platforms: FB
type: text
caption: inactive brand

---

## 2026-10-06 10:00
platforms: FB, IG
type: text
caption: Text on FB, impossible on IG

---

## Fri 2026-10-09
platforms: FB, IG
type: reel
media: clip.MOV
caption fb: Facebook only words
caption li: ignored
"""

NO_BRAND_PLAN = """\
## 2026-10-01 10:00
platforms: FB
type: text
caption: who am I
"""

CASES = {
    "importer": IMPORTER_PLAN,
    "edge": EDGE_PLAN,
    "no-brand": NO_BRAND_PLAN,
    "empty": "# nothing here\n\nbrand: grandinvitation\n",
}


def python_side(plan: str) -> dict:
    brands_list, _, _ = parse_brands(BRANDS_HEADERS, BRAND_ROWS)
    brands = {b.slug: b for b in brands_list if b.active}
    existing: dict = {}
    for slug, t in TABS.items():
        if slug not in brands:
            continue
        for index, row in enumerate(t["rows"]):
            post = parse_post(brands[slug], t["headers"], row, index + 2, TZ)
            if post is None or post.scheduled_at is None:
                continue
            existing[existing_key(post)] = post.post_id or f"row {index + 2}"
    posts, problems = parse_plan(plan)
    reviews = review_plan(posts, brands, set(FILES), existing, TZ, NOW)
    return {
        "brands": sorted(brands),
        "problems": problems,
        "reviews": [
            {
                "post": dataclasses.asdict(r.post),
                "errors": r.errors,
                "platform_errors": r.platform_errors,
                "warnings": r.warnings,
                "captions": r.captions,
                "duplicate_of": r.duplicate_of,
                "importable": r.importable,
                "scheduled_at": r.scheduled_at.isoformat() if r.scheduled_at else None,
            }
            for r in reviews
        ],
    }


def js_side(plan: str) -> dict:
    case = {
        "plan": plan,
        "brands": {"headers": BRANDS_HEADERS, "rows": BRAND_ROWS},
        "tabs": TABS,
        "files": FILES,
        "tz": TZ,
        "now_ms": int(NOW.timestamp() * 1000),
    }
    out = subprocess.run(
        [NODE, str(HARNESS)], input=json.dumps(case), capture_output=True, text=True, check=True
    )
    data = json.loads(out.stdout)
    reviews = []
    for r in data["reviews"]:
        at = r.pop("scheduled_at")
        r["scheduled_at"] = (
            dt.datetime.fromtimestamp(at / 1000, dt.UTC).isoformat() if at is not None else None
        )
        reviews.append(r)
    return {"brands": sorted(data["brands"]), "problems": data["problems"], "reviews": reviews}


@pytest.mark.parametrize("name", sorted(CASES))
def test_js_reviews_plans_exactly_like_python(name: str) -> None:
    py, js = python_side(CASES[name]), js_side(CASES[name])
    assert js["brands"] == py["brands"]
    assert js["problems"] == py["problems"]
    assert len(js["reviews"]) == len(py["reviews"])
    for index, (a, b) in enumerate(zip(py["reviews"], js["reviews"], strict=True)):
        assert b == a, f"{name} post #{index + 1} ({a['post']['heading']!r}) differs"


def test_the_edge_plan_really_exercises_the_edges() -> None:
    """Guard against the parity test passing because both sides say 'ready'."""
    reviews = python_side(EDGE_PLAN)["reviews"]
    errors = " | ".join(e for r in reviews for e in r["errors"])
    for expected in ("unreadable time", "unreadable date", "in the past", "not an image or video",
                     "is not in the files", "unknown brand", "no date", "takes images only",
                     "must start with http", "unknown platform"):
        assert expected in errors, expected
    assert any(r["duplicate_of"] == "gi-0001" for r in reviews)
    assert any(r["duplicate_of"] == "row 4" for r in reviews)  # no ID yet: labelled by row
    assert any(r["duplicate_of"].startswith("post #") for r in reviews)
    assert any(r["platform_errors"] and r["importable"] for r in reviews)  # "partly ready"
    assert any("day-first" in w for r in reviews for w in r["warnings"]) is False  # plan dates are ISO
    assert any("heading says Tue" in w for r in reviews for w in r["warnings"])
