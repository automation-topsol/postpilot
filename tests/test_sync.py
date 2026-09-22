"""Sync: ID assignment, status roll-up, and the hash rules that protect the
delivery guarantee."""

from __future__ import annotations

import pytest

from postpilot.models import Platform, PlatformState, PostType, RowStatus, StateRow, roll_up_status
from postpilot.sheets.schema import BRANDS_HEADERS, LOG_HEADERS, STATE_HEADERS, brand_headers
from postpilot.sheets.state import state_to_row
from postpilot.sync import next_post_id, sync
from tests.conftest import TZ, make_row
from tests.fakes import FakeSheetClient, state_column

HEADERS = brand_headers()

BRAND_ROW = make_row(
    BRANDS_HEADERS,
    **{
        "Brand Name": "Grand Invitation",
        "Slug": "grandinvitation",
        "Enabled Platforms": "FB, IG",
        "Facebook Page ID": "1355072654348977",
        "Instagram User ID": "17841432916654917",
        "Drive Folder ID": "folder",
        "Default Hashtags": "#GrandInvitation",
        "Active": "TRUE",
    },
)


def build(rows: list[list[str]], *, states: list[StateRow] | None = None, brands=None) -> FakeSheetClient:
    state_rows = [state_to_row(s) for s in (states or [])]
    return FakeSheetClient(
        {
            "_Brands": (BRANDS_HEADERS, brands if brands is not None else [BRAND_ROW]),
            "_State": (STATE_HEADERS, state_rows),
            "_Log": (LOG_HEADERS, []),
            "grandinvitation": (HEADERS, rows),
        }
    )


def post_row(**values: str) -> list[str]:
    return make_row(HEADERS, **values)


class TestIdAssignment:
    def test_assigns_sequential_ids_with_brand_prefix(self):
        client = build([
            post_row(Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="one"),
            post_row(Date="2026-09-26", Platforms="FB", Type="image", Media="b.jpg", Caption="two"),
        ])
        sync(client, tz_name=TZ)
        assert client.cell("grandinvitation", 2, "ID") == "gi-0001"
        assert client.cell("grandinvitation", 3, "ID") == "gi-0002"

    def test_existing_ids_are_never_reassigned(self):
        client = build([
            post_row(ID="gi-0042", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="x"),
        ])
        sync(client, tz_name=TZ)
        assert client.cell("grandinvitation", 2, "ID") == "gi-0042"

    def test_new_ids_continue_past_the_highest_existing(self):
        client = build([
            post_row(ID="gi-0042", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="x"),
            post_row(Date="2026-09-26", Platforms="FB", Type="image", Media="b.jpg", Caption="y"),
        ])
        sync(client, tz_name=TZ)
        assert client.cell("grandinvitation", 3, "ID") == "gi-0043"

    def test_draft_rows_get_no_id(self):
        client = build([post_row(Caption="just a thought")])
        result = sync(client, tz_name=TZ)
        assert client.cell("grandinvitation", 2, "ID") == ""
        assert result.post_count == 0

    def test_next_post_id_ignores_other_prefixes(self):
        assert next_post_id("gi", {"rp-0099", "gi-0007"}) == "gi-0008"

    def test_rows_are_keyed_by_id_not_position(self):
        """Reordering rows must not move state between posts."""
        first = post_row(ID="gi-0001", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="A")
        second = post_row(ID="gi-0002", Date="2026-09-26", Platforms="FB", Type="image", Media="b.jpg", Caption="B")
        forwards = build([first, second])
        sync(forwards, tz_name=TZ)
        backwards = build([second, first])
        sync(backwards, tz_name=TZ)
        assert state_column(forwards, "gi-0001", "FB", "Content Hash") == \
               state_column(backwards, "gi-0001", "FB", "Content Hash")


class TestStateCreation:
    def test_one_state_row_per_post_and_platform(self):
        client = build([
            post_row(ID="gi-1", Date="2026-09-25", Platforms="FB, IG", Type="image", Media="a.jpg", Caption="x"),
        ])
        sync(client, tz_name=TZ)
        assert state_column(client, "gi-1", "FB", "State") == "scheduled"
        assert state_column(client, "gi-1", "IG", "State") == "scheduled"

    def test_invalid_platform_is_recorded_per_platform(self):
        client = build([
            post_row(ID="gi-2", Date="2026-09-25", Platforms="FB, IG", Type="text", Caption="words"),
        ])
        sync(client, tz_name=TZ)
        # text + IG invalidates IG only — FB still publishes.
        assert state_column(client, "gi-2", "FB", "State") == "scheduled"
        assert state_column(client, "gi-2", "IG", "State") == "invalid"

    def test_deleted_rows_drop_their_state(self):
        existing = StateRow(post_id="gi-9", brand_slug="grandinvitation", platform=Platform.FB)
        client = build([], states=[existing])
        sync(client, tz_name=TZ)
        assert client.replaced["_State"] == []


class TestHashRules:
    def _published(self, content_hash: str) -> StateRow:
        return StateRow(
            post_id="gi-1",
            brand_slug="grandinvitation",
            platform=Platform.FB,
            state=PlatformState.PUBLISHED,
            content_hash=content_hash,
            remote_url="https://facebook.com/123",
        )

    def test_edit_after_publish_never_republishes(self):
        client = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="EDITED")],
            states=[self._published("staleHash00000000")],
        )
        sync(client, tz_name=TZ)
        # THE guarantee: a published platform is never returned to scheduled.
        assert state_column(client, "gi-1", "FB", "State") == "published"

    def test_edit_after_publish_warns_in_notes(self):
        client = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="EDITED")],
            states=[self._published("staleHash00000000")],
        )
        sync(client, tz_name=TZ)
        assert "differs from the Sheet" in client.cell("grandinvitation", 2, "Notes")

    def test_published_hash_is_not_overwritten_so_the_warning_persists(self):
        client = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="EDITED")],
            states=[self._published("staleHash00000000")],
        )
        sync(client, tz_name=TZ)
        assert state_column(client, "gi-1", "FB", "Content Hash") == "staleHash00000000"

    @pytest.mark.parametrize(
        "state", [PlatformState.PERMANENT_FAILED, PlatformState.RETRYABLE_FAILED, PlatformState.INVALID]
    )
    def test_corrected_row_reopens_failed_platforms(self, state):
        stale = StateRow(
            post_id="gi-1", brand_slug="grandinvitation", platform=Platform.FB,
            state=state, attempts=3, content_hash="oldHash0000000000", last_error="boom",
        )
        client = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="image", Media="fixed.jpg", Caption="x")],
            states=[stale],
        )
        sync(client, tz_name=TZ)
        assert state_column(client, "gi-1", "FB", "State") == "scheduled"
        assert state_column(client, "gi-1", "FB", "Attempts") == "0"
        assert state_column(client, "gi-1", "FB", "Last Error") == ""

    def test_skipped_stays_skipped_even_when_edited(self):
        stale = StateRow(
            post_id="gi-1", brand_slug="grandinvitation", platform=Platform.FB,
            state=PlatformState.SKIPPED, content_hash="oldHash0000000000",
        )
        client = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="image", Media="new.jpg", Caption="x")],
            states=[stale],
        )
        sync(client, tz_name=TZ)
        assert state_column(client, "gi-1", "FB", "State") == "skipped"

    def test_fixing_invalid_media_name_reschedules_automatically(self):
        bad = build([post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="carousel",
                              Media="only-one.jpg", Caption="x")])
        sync(bad, tz_name=TZ)
        assert state_column(bad, "gi-1", "FB", "State") == "invalid"

        fixed = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="carousel",
                      Media="one.jpg, two.jpg", Caption="x")],
            states=[StateRow(post_id="gi-1", brand_slug="grandinvitation", platform=Platform.FB,
                             state=PlatformState.INVALID, content_hash="oldHash0000000000",
                             last_error="carousel needs 2-10")],
        )
        sync(fixed, tz_name=TZ)
        assert state_column(fixed, "gi-1", "FB", "State") == "scheduled"

    def test_unchanged_row_is_left_alone(self):
        client = build([post_row(ID="gi-1", Date="2026-09-25", Platforms="FB",
                                 Type="image", Media="a.jpg", Caption="x")])
        sync(client, tz_name=TZ)
        settled = state_column(client, "gi-1", "FB", "Content Hash")

        again = build(
            [post_row(ID="gi-1", Date="2026-09-25", Platforms="FB", Type="image", Media="a.jpg", Caption="x")],
            states=[StateRow(post_id="gi-1", brand_slug="grandinvitation", platform=Platform.FB,
                             state=PlatformState.RETRYABLE_FAILED, attempts=2, content_hash=settled)],
        )
        sync(again, tz_name=TZ)
        # No content change, so no free retry: attempts are preserved.
        assert state_column(again, "gi-1", "FB", "State") == "retryable_failed"
        assert state_column(again, "gi-1", "FB", "Attempts") == "2"


class TestStatusRollUp:
    def _state(self, platform: Platform, state: PlatformState) -> StateRow:
        return StateRow(post_id="p", brand_slug="b", platform=platform, state=state)

    def test_draft_wins_when_unscheduled(self):
        assert roll_up_status([], is_draft=True, has_issues=False) is RowStatus.DRAFT

    def test_all_published(self):
        states = [self._state(Platform.FB, PlatformState.PUBLISHED),
                  self._state(Platform.IG, PlatformState.PUBLISHED)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.PUBLISHED

    def test_one_published_one_failed_is_partial(self):
        states = [self._state(Platform.FB, PlatformState.PUBLISHED),
                  self._state(Platform.IG, PlatformState.RETRYABLE_FAILED)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.PARTIAL

    def test_unknown_outranks_everything(self):
        states = [self._state(Platform.FB, PlatformState.PUBLISHED),
                  self._state(Platform.IG, PlatformState.UNKNOWN)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.NEEDS_REVIEW

    def test_invalid_only_when_nothing_is_publishable(self):
        both = [self._state(Platform.FB, PlatformState.INVALID),
                self._state(Platform.IG, PlatformState.INVALID)]
        assert roll_up_status(both, is_draft=False, has_issues=True) is RowStatus.INVALID

        mixed = [self._state(Platform.FB, PlatformState.SCHEDULED),
                 self._state(Platform.IG, PlatformState.INVALID)]
        assert roll_up_status(mixed, is_draft=False, has_issues=True) is RowStatus.SCHEDULED

    def test_skipped_platforms_do_not_drag_the_row_down(self):
        states = [self._state(Platform.FB, PlatformState.PUBLISHED),
                  self._state(Platform.IG, PlatformState.SKIPPED)]
        assert roll_up_status(states, is_draft=False, has_issues=False) is RowStatus.PUBLISHED


class TestWrites:
    def test_tool_columns_are_written_as_one_contiguous_range(self):
        client = build([post_row(ID="gi-1", Date="2026-09-25", Platforms="FB",
                                 Type="image", Media="a.jpg", Caption="x")])
        sync(client, tz_name=TZ)
        ranges = client.written_ranges("grandinvitation")
        # Six tool columns must cost ONE range, not six writes — the Sheets
        # quota is the reason this module exists.
        assert "M2:R2" in ranges

    def test_dry_run_writes_nothing(self):
        client = build([post_row(Date="2026-09-25", Platforms="FB", Type="image",
                                 Media="a.jpg", Caption="x")])
        result = sync(client, tz_name=TZ, write=False)
        assert client.flushed == []
        assert client.replaced == {}
        assert result.post_count == 1

    def test_status_column_reflects_the_roll_up(self):
        client = build([post_row(ID="gi-1", Date="2026-09-25", Platforms="FB, IG",
                                 Type="text", Caption="words")])
        sync(client, tz_name=TZ)
        assert client.cell("grandinvitation", 2, "Status") == RowStatus.SCHEDULED.value
        assert "Instagram cannot post without media" in client.cell("grandinvitation", 2, "Error")


class TestCaptions:
    def test_per_platform_override_wins(self, brand):
        from postpilot.models import Post

        post = Post(post_id="p", brand_slug="b", row_number=2, caption="shared",
                    caption_instagram="ig only", platforms=[Platform.FB, Platform.IG],
                    post_type=PostType.IMAGE)
        assert post.caption_for(Platform.FB, brand) == "shared"
        assert post.caption_for(Platform.IG, brand).startswith("ig only")

    def test_link_appended_for_fb_and_li_but_not_ig(self, brand):
        from postpilot.models import Post

        post = Post(post_id="p", brand_slug="b", row_number=2, caption="text",
                    link="https://example.com", platforms=[Platform.FB, Platform.IG])
        assert "https://example.com" in post.caption_for(Platform.FB, brand)
        assert "https://example.com" not in post.caption_for(Platform.IG, brand)

    def test_default_hashtags_only_for_instagram_and_only_when_absent(self, brand):
        from postpilot.models import Post

        plain = Post(post_id="p", brand_slug="b", row_number=2, caption="no tags here")
        assert brand.default_hashtags in plain.caption_for(Platform.IG, brand)
        assert brand.default_hashtags not in plain.caption_for(Platform.FB, brand)

        own = Post(post_id="p", brand_slug="b", row_number=2, caption="mine #wedding")
        assert brand.default_hashtags not in own.caption_for(Platform.IG, brand)
