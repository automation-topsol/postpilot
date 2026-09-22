"""Row parsing: forgiving about shape, strict about meaning."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from postpilot.models import MEDIA_COUNTS, Platform, PostType
from postpilot.sheets.parse import (
    parse_bool,
    parse_brands,
    parse_media,
    parse_platforms,
    parse_post,
    parse_schedule,
)
from tests.conftest import TZ, make_row


class TestSchedule:
    def test_iso_date_converts_karachi_to_utc(self):
        # Asia/Karachi is UTC+5 year-round; 18:30 local is 13:30 UTC.
        when, error, warning = parse_schedule("2026-09-25", "18:30", TZ)
        assert when == dt.datetime(2026, 9, 25, 13, 30, tzinfo=dt.UTC)
        assert error is None and warning is None

    def test_missing_time_means_midnight(self):
        when, error, _ = parse_schedule("2026-09-25", "", TZ)
        assert when == dt.datetime(2026, 9, 24, 19, 0, tzinfo=dt.UTC)
        assert error is None

    def test_no_date_is_draft_not_error(self):
        assert parse_schedule("", "18:30", TZ) == (None, None, None)

    @pytest.mark.parametrize("text", ["6:05 PM", "18:05", "18:05:00"])
    def test_time_formats(self, text):
        when, error, _ = parse_schedule("2026-09-25", text, TZ)
        assert error is None
        assert when.astimezone(dt.UTC).hour == 13 and when.minute == 5

    @staticmethod
    def _local_date(when: dt.datetime) -> dt.date:
        """Compare in the Sheet's timezone — the UTC value is a day earlier
        for anything before 05:00 local, which is not a parsing bug."""
        return when.astimezone(ZoneInfo(TZ)).date()

    def test_ambiguous_slash_date_warns_rather_than_guessing_silently(self):
        when, error, warning = parse_schedule("03/04/2026", "00:00", TZ)
        assert error is None
        assert self._local_date(when) == dt.date(2026, 4, 3)  # day-first
        assert warning and "day-first" in warning

    def test_unambiguous_slash_date_does_not_warn(self):
        when, error, warning = parse_schedule("25/09/2026", "00:00", TZ)
        assert error is None and warning is None
        assert self._local_date(when) == dt.date(2026, 9, 25)

    def test_month_first_detected_when_day_exceeds_twelve(self):
        when, _, warning = parse_schedule("09/25/2026", "00:00", TZ)
        assert self._local_date(when) == dt.date(2026, 9, 25)
        assert warning is None

    def test_unreadable_date_is_an_error(self):
        when, error, _ = parse_schedule("next tuesday", "", TZ)
        assert when is None and "unreadable date" in error

    def test_unreadable_time_is_an_error(self):
        when, error, _ = parse_schedule("2026-09-25", "half past six", TZ)
        assert when is None and "unreadable time" in error


class TestScalars:
    @pytest.mark.parametrize("text", ["TRUE", "true", "Yes", " y ", "1"])
    def test_truthy(self, text):
        assert parse_bool(text) is True

    @pytest.mark.parametrize("text", ["FALSE", "no", "0", ""])
    def test_falsy(self, text):
        assert parse_bool(text) is False

    def test_platforms_are_case_and_separator_tolerant(self):
        platforms, unknown = parse_platforms(" fb , IG/li ")
        assert platforms == [Platform.FB, Platform.IG, Platform.LI]
        assert unknown == []

    def test_unknown_platforms_are_reported_not_dropped(self):
        platforms, unknown = parse_platforms("FB, Twitter")
        assert platforms == [Platform.FB] and unknown == ["Twitter"]

    def test_duplicate_platforms_collapse(self):
        platforms, _ = parse_platforms("FB, FB, IG")
        assert platforms == [Platform.FB, Platform.IG]

    def test_media_preserves_order_because_it_is_carousel_order(self):
        assert parse_media("c.jpg, a.jpg, b.jpg") == ["c.jpg", "a.jpg", "b.jpg"]

    def test_media_filenames_may_contain_spaces(self):
        assert parse_media("save the date.jpg, venue.jpg") == ["save the date.jpg", "venue.jpg"]

    def test_drive_links_reduce_to_file_id(self):
        link = "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view?usp=sharing"
        assert parse_media(link) == ["1AbCdEfGhIjKlMnOpQrStUvWxYz012345"]


class TestBrands:
    def test_parses_and_flags_missing_ids(self, brands_headers):
        rows = [
            make_row(brands_headers, **{"Brand Name": "Grand Invitation", "Slug": "grandinvitation",
                                        "Enabled Platforms": "FB, IG", "Facebook Page ID": "1",
                                        "Instagram User ID": "2", "Active": "TRUE"}),
            make_row(brands_headers, **{"Brand Name": "No Page", "Slug": "nopage",
                                        "Enabled Platforms": "FB", "Active": "TRUE"}),
        ]
        brands, problems, warnings = parse_brands(brands_headers, rows)
        assert [b.slug for b in brands] == ["grandinvitation", "nopage"]
        assert problems == []
        assert any("Facebook is enabled but has no ID/URN" in w for w in warnings)

    def test_blank_rows_are_skipped(self, brands_headers):
        rows = [[""] * len(brands_headers)]
        brands, problems, _ = parse_brands(brands_headers, rows)
        assert brands == [] and problems == []

    def test_duplicate_slugs_are_reported(self, brands_headers):
        rows = [
            make_row(brands_headers, **{"Brand Name": "A", "Slug": "dup", "Enabled Platforms": "FB",
                                        "Facebook Page ID": "1"}),
            make_row(brands_headers, **{"Brand Name": "B", "Slug": "dup", "Enabled Platforms": "FB",
                                        "Facebook Page ID": "2"}),
        ]
        _, problems, _ = parse_brands(brands_headers, rows)
        assert any("duplicate slug" in p for p in problems)

    def test_colliding_id_prefixes_are_reported(self, brands_headers):
        # Both yield prefix "gi" — post IDs would collide silently.
        rows = [
            make_row(brands_headers, **{"Brand Name": "Grand Invitation", "Slug": "a",
                                        "Enabled Platforms": "FB", "Facebook Page ID": "1"}),
            make_row(brands_headers, **{"Brand Name": "Global Imports", "Slug": "b",
                                        "Enabled Platforms": "FB", "Facebook Page ID": "2"}),
        ]
        _, problems, _ = parse_brands(brands_headers, rows)
        assert any("post-ID prefix" in p for p in problems)


class TestPostValidation:
    def _post(self, brand, headers, **values):
        row = make_row(headers, **values)
        return parse_post(brand, headers, row, 2, TZ)

    def test_blank_row_returns_none(self, brand, headers):
        assert parse_post(brand, headers, [""] * len(headers), 2, TZ) is None

    def test_valid_image_post_has_no_issues(self, brand, headers):
        post = self._post(brand, headers, ID="gi-0001", Date="2026-09-25", Time="18:30",
                          Platforms="FB, IG", Type="image", Media="a.jpg", Caption="hello")
        assert post.issues == []
        assert post.post_type is PostType.IMAGE

    def test_row_with_no_date_is_draft(self, brand, headers):
        post = self._post(brand, headers, Caption="still writing this")
        assert post.is_draft

    @pytest.mark.parametrize(
        ("post_type", "count", "ok"),
        [("image", 1, True), ("image", 2, False), ("carousel", 1, False), ("carousel", 2, True),
         ("carousel", 10, True), ("carousel", 11, False), ("reel", 1, True), ("text", 0, True),
         ("text", 1, False)],
    )
    def test_media_counts_are_strict(self, brand, headers, post_type, count, ok):
        media = ", ".join(f"f{i}.jpg" for i in range(count))
        post = self._post(brand, headers, ID="gi-1", Date="2026-09-25", Platforms="FB",
                          Type=post_type, Media=media, Caption="x")
        has_count_issue = any("media file" in str(i) for i in post.issues)
        assert has_count_issue is (not ok)

    def test_media_count_bounds_match_the_spec(self):
        assert MEDIA_COUNTS[PostType.CAROUSEL] == (2, 10)

    def test_text_post_invalidates_instagram_only(self, brand, headers):
        post = self._post(brand, headers, ID="gi-2", Date="2026-09-25", Platforms="FB, IG",
                          Type="text", Caption="words only")
        # This is the rule that must never regress: IG's limitation must not
        # stop Facebook publishing the same row.
        assert not post.is_valid_for(Platform.IG)
        assert post.is_valid_for(Platform.FB)

    def test_platform_outside_brand_enabled_is_flagged_for_that_platform_only(self, brand, headers):
        post = self._post(brand, headers, ID="gi-3", Date="2026-09-25", Platforms="FB, LI",
                          Type="image", Media="a.jpg", Caption="x")
        assert not post.is_valid_for(Platform.LI)
        assert post.is_valid_for(Platform.FB)

    def test_bad_type_is_a_row_level_issue(self, brand, headers):
        post = self._post(brand, headers, ID="gi-4", Date="2026-09-25", Platforms="FB",
                          Type="story", Media="a.jpg", Caption="x")
        assert any("is not one of" in str(i) for i in post.issues)
        assert not post.is_valid_for(Platform.FB)

    def test_non_http_link_is_rejected(self, brand, headers):
        post = self._post(brand, headers, ID="gi-5", Date="2026-09-25", Platforms="FB",
                          Type="image", Media="a.jpg", Caption="x", Link="www.example.com")
        assert any("must start with http" in str(i) for i in post.issues)

    def test_missing_caption_on_media_post_warns_but_does_not_block(self, brand, headers):
        post = self._post(brand, headers, ID="gi-6", Date="2026-09-25", Platforms="FB",
                          Type="image", Media="a.jpg")
        assert post.issues == []
        assert any("no caption" in w for w in post.warnings)

    def test_linkedin_only_brand_row_is_valid_even_though_unpublishable(self, li_brand, headers):
        # restocklypos: LinkedIn access is pending, but a well-formed row is
        # still a valid row. It must not be stamped invalid.
        row = make_row(headers, ID="rp-1", Date="2026-09-25", Platforms="LI",
                       Type="image", Media="a.jpg", Caption="x")
        post = parse_post(li_brand, headers, row, 2, TZ)
        assert post.issues == []
        assert post.is_valid_for(Platform.LI)
