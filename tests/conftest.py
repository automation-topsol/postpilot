"""Shared fixtures. No test touches a real API or Google — fakes only."""

from __future__ import annotations

import datetime as dt

import pytest

from postpilot.models import Brand, Platform
from postpilot.sheets.schema import BRANDS_HEADERS, brand_headers

TZ = "Asia/Karachi"


@pytest.fixture
def brand() -> Brand:
    return Brand(
        name="Grand Invitation",
        slug="grandinvitation",
        enabled_platforms=[Platform.FB, Platform.IG],
        facebook_page_id="1355072654348977",
        instagram_user_id="17841432916654917",
        drive_folder_id="13M2wXeZlKX71PCsm3qLkPbIe5VbmALBi",
        default_hashtags="#GrandInvitation #DigitalInvites",
    )


@pytest.fixture
def li_brand() -> Brand:
    """LinkedIn-only, mirroring restocklypos: real, but not yet publishable."""
    return Brand(
        name="Restockly POS",
        slug="restocklypos",
        enabled_platforms=[Platform.LI],
        linkedin_org_urn="urn:li:organization:12345",
        drive_folder_id="1LEf9Dqlm7GefhpzPRMI8HFR4hXbrqV-Z",
    )


@pytest.fixture
def headers() -> list[str]:
    return brand_headers()


@pytest.fixture
def brands_headers() -> list[str]:
    return BRANDS_HEADERS


def make_row(headers: list[str], **values: str) -> list[str]:
    """Build a sheet row from column names, so tests never count columns."""
    row = [""] * len(headers)
    for name, value in values.items():
        label = name.replace("__", " ").replace("_", " ")
        for i, header in enumerate(headers):
            normalised = header.lower().replace("(", "").replace(")", "")
            if normalised == label.lower() or header.lower() == label.lower():
                row[i] = value
                break
        else:
            raise KeyError(f"no column matching {name!r} in {headers}")
    return row


@pytest.fixture
def now() -> dt.datetime:
    return dt.datetime(2026, 9, 23, 12, 0, tzinfo=dt.UTC)
