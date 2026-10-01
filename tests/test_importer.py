"""The weekly plan importer: Markdown parsing, review, Drive, Sheet append, UI server.

No Google, ever: the Sheet is `FakeSheetClient`, Drive is `FakeDriveService`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

import pytest

from postpilot.importer import (
    DriveUploader,
    drive_name_for,
    existing_posts,
    import_reviews,
    load_brands,
)
from postpilot.plan import PlannedPost, parse_plan, review_plan
from postpilot.sheets.parse import parse_post
from postpilot.sheets.schema import brand_headers
from tests.test_publish import BRAND, build, post_row

TZ = "Asia/Karachi"
NOW = dt.datetime(2026, 9, 25, 6, 0, tzinfo=dt.UTC)  # well before the plan's dates
BRANDS = {BRAND.slug: BRAND}

PLAN = """\
# Weekly plan

<!--
brand:      the brand's slug      <- must NOT be read as a default
-->

brand: grandinvitation

## Mon 2026-09-28 18:30
platforms: FB, IG
type: carousel
media: gold-1.png, gold-2.png
link: https://grandinvitation.com/gold

caption:
Our new Gold collection is here.
link: this line is caption text, not a field

caption ig:
Gold ✨ #wedding

---

## Wed 2026-09-30 6:00 PM
platforms: FB, IG
type: text

caption:
Closed on Saturday.

---

## Fri 2026-10-02 12:00
brand: someoneelse
platforms: FB
type: image
media: promo.png
caption: One-line caption on the key line.
"""


def reviews_for(text: str, files=("gold-1.png", "gold-2.png", "promo.png"), existing=None, now=NOW):
    posts, problems = parse_plan(text)
    return review_plan(posts, BRANDS, set(files), existing or {}, TZ, now=now), problems


# --------------------------------------------------------------------------
class TestParse:
    def test_reads_every_post_and_field(self):
        posts, problems = parse_plan(PLAN)
        assert problems == []
        assert len(posts) == 3
        first = posts[0]
        assert (first.brand, first.date, first.time) == ("grandinvitation", "2026-09-28", "18:30")
        assert first.platforms == "FB, IG" and first.type == "carousel"
        assert first.media == ["gold-1.png", "gold-2.png"]
        assert first.link == "https://grandinvitation.com/gold"
        assert first.caption_instagram == "Gold ✨ #wedding"

    def test_caption_lines_that_look_like_fields_stay_in_the_caption(self):
        first = parse_plan(PLAN)[0][0]
        assert "link: this line is caption text" in first.caption
        assert first.link == "https://grandinvitation.com/gold"

    def test_comments_are_ignored(self):
        # The template's own docs contain "brand: ..." — that must not become
        # the default brand.
        assert parse_plan(PLAN)[0][0].brand == "grandinvitation"

    def test_twelve_hour_time_in_heading_becomes_24_hour(self):
        assert parse_plan(PLAN)[0][1].time == "18:00"

    def test_unreadable_time_is_left_for_review_to_report(self):
        post = parse_plan("brand: grandinvitation\n## 2026-10-01\ntime: teatime\ntype: text\nplatforms: FB\ncaption: x\n")[0][0]
        assert post.time == "teatime"
        review = review_plan([post], BRANDS, set(), {}, TZ, now=NOW)[0]
        assert any("unreadable time" in e for e in review.errors)

    def test_per_post_brand_overrides_default(self):
        assert parse_plan(PLAN)[0][2].brand == "someoneelse"

    def test_caption_on_the_key_line(self):
        assert parse_plan(PLAN)[0][2].caption == "One-line caption on the key line."

    def test_empty_plan_says_what_a_post_looks_like(self):
        posts, problems = parse_plan("just some notes\n")
        assert posts == [] and "## YYYY-MM-DD HH:MM" in problems[0]

    def test_heading_is_optional_separator_after_rule(self):
        posts, _ = parse_plan("## 2026-10-01 10:00\nbrand: x\n---\n## 2026-10-02 10:00\nbrand: y\n")
        assert [p.brand for p in posts] == ["x", "y"]


# --------------------------------------------------------------------------
class TestReview:
    def test_valid_post_is_importable_with_final_captions(self):
        reviews, _ = reviews_for(PLAN)
        first = reviews[0]
        assert first.importable and not first.errors
        assert first.captions["FB"].endswith("https://grandinvitation.com/gold")
        assert "https://" not in first.captions["IG"]  # IG ignores links

    def test_text_post_on_ig_blocks_only_ig(self):
        second = reviews_for(PLAN)[0][1]
        assert "IG" in second.platform_errors and "FB" not in second.platform_errors
        assert second.importable  # FB still goes

    def test_unknown_brand_blocks(self):
        third = reviews_for(PLAN)[0][2]
        assert not third.importable and "unknown brand" in third.errors[0]

    def test_missing_file_blocks(self):
        first = reviews_for(PLAN, files=("gold-1.png",))[0][0]
        assert any("gold-2.png" in e for e in first.errors) and not first.importable

    def test_past_time_blocks(self):
        # A plan imported late must not publish a week of posts at once.
        late = dt.datetime(2026, 10, 1, 0, 0, tzinfo=dt.UTC)
        first = reviews_for(PLAN, now=late)[0][0]
        assert any("in the past" in e for e in first.errors) and not first.importable

    def test_video_in_a_carousel_blocks(self):
        plan = "brand: grandinvitation\n## 2026-10-01 10:00\nplatforms: FB\ntype: carousel\nmedia: a.png, b.mp4\ncaption: x\n"
        review = reviews_for(plan, files=("a.png", "b.mp4"))[0][0]
        assert any("images only" in e for e in review.errors)

    def test_reel_needs_video(self):
        plan = "brand: grandinvitation\n## 2026-10-01 10:00\nplatforms: IG\ntype: reel\nmedia: a.png\ncaption: x\n"
        review = reviews_for(plan, files=("a.png",))[0][0]
        assert any("reel takes one video" in e for e in review.errors)

    def test_wrong_count_uses_the_sheets_own_message(self):
        plan = "brand: grandinvitation\n## 2026-10-01 10:00\nplatforms: FB\ntype: carousel\nmedia: a.png\ncaption: x\n"
        review = reviews_for(plan, files=("a.png",))[0][0]
        assert any("carousel needs 2-10" in e for e in review.errors)

    def test_review_agrees_with_sync_parsing(self):
        # The importer writes exactly the row it validated.
        post = parse_plan(PLAN)[0][0]
        parsed = parse_post(BRAND, brand_headers(), post.row_for(brand_headers()), 2, TZ)
        assert parsed.media == ["gold-1.png", "gold-2.png"]
        assert parsed.scheduled_at == dt.datetime(2026, 9, 28, 13, 30, tzinfo=dt.UTC)

    def test_wrong_weekday_in_heading_warns(self):
        plan = "brand: grandinvitation\n## Tue 2026-10-05 10:00\nplatforms: FB\ntype: text\ncaption: x\n"
        review = reviews_for(plan)[0][0]
        assert any("2026-10-05 is a Monday" in w for w in review.warnings)
        assert review.importable  # a warning, not a block

    def test_right_weekday_is_quiet(self):
        plan = "brand: grandinvitation\n## Monday 2026-10-05 10:00\nplatforms: FB\ntype: text\ncaption: x\n"
        assert reviews_for(plan)[0][0].warnings == []


class TestDuplicates:
    def test_already_in_sheet_is_flagged_not_imported(self):
        post = parse_plan(PLAN)[0][0]
        client = build([post_row(ID="gi-0009", **_cells(post))])
        existing = existing_posts(client, BRANDS, TZ)
        first = reviews_for(PLAN, existing=existing)[0][0]
        assert first.duplicate_of == "gi-0009" and not first.importable

    def test_row_without_id_is_labelled_by_row(self):
        post = parse_plan(PLAN)[0][0]
        client = build([post_row(ID="", **_cells(post))])
        assert next(iter(existing_posts(client, BRANDS, TZ).values())) == "row 2"

    def test_same_post_twice_in_one_plan(self):
        block = "## 2026-10-01 10:00\nplatforms: FB\ntype: text\ncaption: Same\n"
        reviews, _ = reviews_for("brand: grandinvitation\n" + block + "---\n" + block)
        assert reviews[0].importable
        assert reviews[1].duplicate_of == "post #1 in this plan"

    def test_a_series_at_the_same_time_is_not_a_duplicate(self):
        # Full captions, never prefixes (adversarial defect #2).
        plan = (
            "brand: grandinvitation\n"
            "## 2026-10-01 10:00\nplatforms: FB\ntype: text\ncaption: Our story, Part ONE\n---\n"
            "## 2026-10-01 10:00\nplatforms: FB\ntype: text\ncaption: Our story, Part TWO\n"
        )
        reviews, _ = reviews_for(plan)
        assert all(r.importable for r in reviews)


def _cells(post: PlannedPost) -> dict[str, str]:
    values = post.sheet_values()
    return {
        "Date": values["Date"], "Time": values["Time"], "Platforms": values["Platforms"],
        "Type": values["Type"], "Media": values["Media"], "Caption": values["Caption"],
        "Caption__Instagram": values["Caption (Instagram)"], "Link": values["Link"],
    }


# --------------------------------------------------------------------------
class FakeDriveService:
    """Just enough of the Drive v3 client for `DriveUploader`."""

    def __init__(self, files=None) -> None:
        self.files_in = list(files or [])
        self.created: list[dict] = []
        self._op = None

    def files(self):
        return self

    def list(self, **kwargs):
        self._op = ("list", kwargs)
        return self

    def create(self, body, media_body, **kwargs):
        self._op = ("create", body, media_body)
        return self

    def execute(self):
        kind = self._op[0]
        if kind == "list":
            return {"files": self.files_in}
        _, body, media = self._op
        data = Path(media._filename).read_bytes()
        created = {
            "id": f"new-{len(self.created)}", "name": body["name"], "mimeType": media.mimetype(),
            "size": str(len(data)), "md5Checksum": hashlib.md5(data).hexdigest(),
        }
        self.created.append(created | {"parents": body["parents"]})
        return created


def drive_entry(name: str, data: bytes, file_id: str = "x") -> dict:
    return {"id": file_id, "name": name, "mimeType": "image/png", "size": str(len(data)),
            "md5Checksum": hashlib.md5(data).hexdigest()}


class TestDriveUploader:
    def test_new_file_is_uploaded_with_its_name(self, tmp_path):
        local = tmp_path / "a.png"
        local.write_bytes(b"AAA")
        service = FakeDriveService()
        name, uploaded = DriveUploader(None, service=service).ensure("folder", local)
        assert (name, uploaded) == ("a.png", True)
        assert service.created[0]["parents"] == ["folder"]

    def test_same_bytes_already_there_is_reused_under_its_drive_name(self, tmp_path):
        local = tmp_path / "a.png"
        local.write_bytes(b"AAA")
        service = FakeDriveService([drive_entry("older-name.png", b"AAA")])
        name, uploaded = DriveUploader(None, service=service).ensure("folder", local)
        assert (name, uploaded) == ("older-name.png", False)
        assert service.created == []

    def test_name_clash_with_different_bytes_gets_a_suffix(self, tmp_path):
        # Two files with one name is the "ambiguous" error sync refuses.
        local = tmp_path / "a.png"
        local.write_bytes(b"NEW")
        service = FakeDriveService([drive_entry("a.png", b"OLD")])
        name, uploaded = DriveUploader(None, service=service).ensure("folder", local)
        assert uploaded and name == f"a-{hashlib.md5(b'NEW').hexdigest()[:6]}.png"

    def test_same_bytes_under_an_ambiguous_name_is_not_reused(self, tmp_path):
        local = tmp_path / "a.png"
        local.write_bytes(b"AAA")
        service = FakeDriveService([drive_entry("a.png", b"AAA", "1"), drive_entry("a.png", b"ZZZ", "2")])
        name, uploaded = DriveUploader(None, service=service).ensure("folder", local)
        assert uploaded and name != "a.png"

    def test_a_wanted_name_is_used_and_suffixed_on_a_clash(self, tmp_path):
        local = tmp_path / "slide_01.png"
        local.write_bytes(b"NEW")
        service = FakeDriveService([drive_entry("d02_p01_slide_01.png", b"OLD")])
        name, uploaded = DriveUploader(None, service=service).ensure("folder", local, "d02_p01_slide_01.png")
        assert uploaded and name == f"d02_p01_slide_01-{hashlib.md5(b'NEW').hexdigest()[:6]}.png"

    def test_second_use_in_one_batch_reuses_the_first_upload(self, tmp_path):
        local = tmp_path / "a.png"
        local.write_bytes(b"AAA")
        service = FakeDriveService()
        uploader = DriveUploader(None, service=service)
        uploader.ensure("folder", local)
        assert uploader.ensure("folder", local) == ("a.png", False)
        assert len(service.created) == 1


# --------------------------------------------------------------------------
class TestImport:
    def test_folder_paths_go_to_drive_flat(self, tmp_path):
        assert drive_name_for("d02_p01/slide_01.png") == "d02_p01_slide_01.png"
        assert drive_name_for("promo.png") == "promo.png"

    def test_a_carousel_folder_is_uploaded_flat_and_the_row_says_so(self, tmp_path):
        for n in (1, 2):
            (tmp_path / "d02_p01").mkdir(exist_ok=True)
            (tmp_path / "d02_p01" / f"slide_0{n}.png").write_bytes(f"s{n}".encode())
        plan = PLAN.replace("gold-1.png, gold-2.png", "d02_p01/slide_01.png, d02_p01/slide_02.png")
        client = build([])
        service = FakeDriveService()
        reviews, _ = reviews_for(plan, files={"d02_p01/slide_01.png", "d02_p01/slide_02.png", "promo.png"})
        import_reviews(
            reviews, files_dir=tmp_path, brands=load_brands(client),
            client=client, uploader=DriveUploader(None, service=service),
        )
        assert [c["name"] for c in service.created][:2] == ["d02_p01_slide_01.png", "d02_p01_slide_02.png"]
        tab = client.read("grandinvitation")
        assert tab.rows[0][tab.headers.index("Media")] == "d02_p01_slide_01.png, d02_p01_slide_02.png"

    def setup_files(self, tmp_path: Path) -> Path:
        for name in ("gold-1.png", "gold-2.png", "promo.png"):
            (tmp_path / name).write_bytes(name.encode())
        return tmp_path

    def test_appends_rows_without_ids_and_only_importable_posts(self, tmp_path):
        client = build([])
        brands = load_brands(client)
        reviews, _ = reviews_for(PLAN)
        result = import_reviews(
            reviews, files_dir=self.setup_files(tmp_path), brands=brands,
            client=client, uploader=DriveUploader(None, service=FakeDriveService()),
        )
        assert result.rows_added == {"grandinvitation": 2}  # third post: unknown brand
        assert sorted(result.uploaded) == ["gold-1.png", "gold-2.png"]
        tab = client.read("grandinvitation")
        headers = tab.headers
        assert all(r[headers.index("ID")] == "" for r in tab.rows)  # sync assigns IDs
        assert tab.rows[0][headers.index("Media")] == "gold-1.png, gold-2.png"
        assert any("unknown brand" in s or "has errors" in s for s in result.skipped)

    def test_writes_in_the_real_tabs_column_order(self, tmp_path):
        # Humans reorder columns; the importer must follow the tab, not the schema.
        headers = list(reversed(brand_headers()))
        client = build([])
        client._tabs["grandinvitation"].headers = headers
        reviews, _ = reviews_for(PLAN)
        import_reviews(
            reviews, files_dir=self.setup_files(tmp_path), brands=load_brands(client),
            client=client, uploader=DriveUploader(None, service=FakeDriveService()),
        )
        row = client.read("grandinvitation").rows[0]
        assert row[headers.index("Date")] == "2026-09-28"
        assert row[headers.index("Type")] == "carousel"

    def test_renamed_upload_is_what_the_row_references(self, tmp_path):
        client = build([])
        service = FakeDriveService([drive_entry("gold-1.png", b"something else")])
        reviews, _ = reviews_for(PLAN)
        import_reviews(
            reviews, files_dir=self.setup_files(tmp_path), brands=load_brands(client),
            client=client, uploader=DriveUploader(None, service=service),
        )
        tab = client.read("grandinvitation")
        media = tab.rows[0][tab.headers.index("Media")]
        assert media.startswith("gold-1-") and media.endswith(", gold-2.png")

    def test_removed_in_preview_is_skipped(self, tmp_path):
        client = build([])
        reviews, _ = reviews_for(PLAN)
        reviews[0].post.include = False
        result = import_reviews(
            reviews, files_dir=self.setup_files(tmp_path), brands=load_brands(client),
            client=client, uploader=DriveUploader(None, service=FakeDriveService()),
        )
        assert result.rows_added == {"grandinvitation": 1}
        assert any("removed in the preview" in s for s in result.skipped)

    def test_drive_failure_skips_that_post_only(self, tmp_path):
        class Broken(FakeDriveService):
            def create(self, *a, **k):
                raise RuntimeError("quota")

        client = build([])
        reviews, _ = reviews_for(PLAN)
        result = import_reviews(
            reviews, files_dir=self.setup_files(tmp_path), brands=load_brands(client),
            client=client, uploader=DriveUploader(None, service=Broken()),
        )
        assert any("Drive upload failed" in e for e in result.errors)
        assert result.rows_added == {"grandinvitation": 1}  # the text post still lands

    def test_importing_twice_adds_nothing_the_second_time(self, tmp_path):
        client = build([])
        service = FakeDriveService()
        files = self.setup_files(tmp_path)
        for _ in range(2):
            existing = existing_posts(client, load_brands(client), TZ)
            reviews, _ = reviews_for(PLAN, existing=existing)
            result = import_reviews(
                reviews, files_dir=files, brands=load_brands(client),
                client=client, uploader=DriveUploader(None, service=service),
            )
        assert result.rows_added == {}
        assert len(client.read("grandinvitation").rows) == 2


# --------------------------------------------------------------------------
class TestServer:
    @pytest.fixture
    def app(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        import postpilot.ui.server as server

        client = build([])
        monkeypatch.setattr(server, "BATCH_ROOT", tmp_path)
        monkeypatch.setattr(server, "SheetClient", lambda *a, **k: client)

        class FakeSettings:
            class tunables:
                timezone = TZ

            google_credentials = None
            sheet_id = "sheet"

        return TestClient(server.create_app(FakeSettings()), base_url="http://127.0.0.1")

    def test_drop_plan_and_files_returns_reviews(self, app):
        files = [
            ("files", ("plan.md", PLAN.replace("2026-", "2099-").encode(), "text/markdown")),
            ("files", ("gold-1.png", b"1", "image/png")),
            ("files", ("gold-2.png", b"2", "image/png")),
        ]
        body = app.post("/api/batch", files=files).json()
        assert len(body["reviews"]) == 3
        assert body["reviews"][0]["importable"] is True
        assert body["files"] == ["gold-1.png", "gold-2.png"]

    def test_carousel_folders_keep_their_paths(self, app):
        # Two carousels, each a folder with its own slide_01.png.
        plan = (
            "brand: grandinvitation\n\n## 2099-10-02 18:30\nplatforms: FB, IG\ntype: carousel\n"
            "media: d02_p01/slide_01.png, d02_p01/slide_02.png\ncaption: one\n\n"
            "## 2099-10-03 18:30\nplatforms: FB, IG\ntype: carousel\n"
            "media: d03_p01/slide_01.png, d03_p01/slide_02.png\ncaption: two\n"
        )
        files = [("files", ("output.md", plan.encode(), "text/markdown"))] + [
            ("files", (f"{d}/slide_0{n}.png", f"{d}{n}".encode(), "image/png"))
            for d in ("d02_p01", "d03_p01") for n in (1, 2)
        ]
        body = app.post("/api/batch", files=files).json()
        assert body["files"] == ["d02_p01/slide_01.png", "d02_p01/slide_02.png",
                                 "d03_p01/slide_01.png", "d03_p01/slide_02.png"]
        assert [r["importable"] for r in body["reviews"]] == [True, True]
        thumb = app.get(f"/api/batch/{body['batch']}/file/d03_p01%2Fslide_02.png")
        assert thumb.status_code == 200 and thumb.content == b"d03_p012"

    def test_hidden_files_are_refused(self, app):
        files = [
            ("files", ("plan.md", b"## 2099-01-01 10:00\n", "text/markdown")),
            ("files", ("d02/.DS_Store", b"x", "application/octet-stream")),
        ]
        assert app.post("/api/batch", files=files).status_code == 400

    def test_needs_exactly_one_plan(self, app):
        response = app.post("/api/batch", files=[("files", ("a.png", b"1", "image/png"))])
        assert response.status_code == 400 and "exactly one .md" in response.json()["detail"]

    def test_file_names_cannot_escape_the_batch(self, app):
        files = [
            ("files", ("plan.md", b"## 2099-01-01 10:00\n", "text/markdown")),
            ("files", ("../../evil.png", b"x", "image/png")),
        ]
        body = app.post("/api/batch", files=files).json()
        assert body["files"] == ["evil.png"]

    def test_cross_origin_post_is_refused(self, app):
        files = [("files", ("plan.md", b"x", "text/markdown"))]
        response = app.post("/api/batch", files=files, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403

    def test_non_local_host_is_refused(self, app):
        # DNS rebinding: a remote name pointed at 127.0.0.1.
        assert app.get("/api/status", headers={"Host": "evil.example"}).status_code == 403

    def test_import_requires_google_sign_in(self, app, monkeypatch):
        import postpilot.auth.google as google

        monkeypatch.setattr(google, "load_credentials", lambda *a, **k: None)
        files = [("files", ("plan.md", b"## 2099-01-01 10:00\n", "text/markdown"))]
        batch = app.post("/api/batch", files=files).json()["batch"]
        response = app.post(f"/api/batch/{batch}/import", json={"posts": []})
        assert response.status_code == 401
