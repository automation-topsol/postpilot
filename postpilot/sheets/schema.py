"""Tab layouts. Column order here IS the Sheet's column order.

Columns are always addressed by *header name* when reading, never by position,
because humans reorder and insert columns. The positional constants below are
used only when writing back and when building Sheets API requests.
"""

from __future__ import annotations

from postpilot.models import HumanAction, PostType

BRANDS_TAB = "_Brands"
STATE_TAB = "_State"
LOG_TAB = "_Log"

# Tabs the tool owns outright. A brand tab is anything else.
RESERVED_TABS = {BRANDS_TAB, STATE_TAB, LOG_TAB}

BRANDS_HEADERS = [
    "Brand Name",
    "Slug",
    "Enabled Platforms",
    "Facebook Page ID",
    "Instagram User ID",
    "LinkedIn Org URN",
    "Drive Folder ID",
    "Default Hashtags",
    "Active",
]

# Columns the teammate fills in.
BRAND_USER_COLUMNS = [
    "ID",
    "Date",
    "Time",
    "Platforms",
    "Type",
    "Media",
    "Caption",
    "Caption (Facebook)",
    "Caption (Instagram)",
    "Caption (LinkedIn)",
    "Link",
    "Action",
]

# Columns the tool writes. Protected, so an accidental edit warns first.
BRAND_TOOL_COLUMNS = [
    "Status",
    "Published URLs",
    "Error",
    "Attempts",
    "Last Run",
    "Notes",
]

STATE_HEADERS = [
    "Post ID",
    "Brand",
    "Platform",
    "State",
    "Attempts",
    "Attempt ID",
    "Remote ID",
    "Remote URL",
    "Content Hash",
    "Last Error",
    "Started At",
    "Last Attempt At",
    "Next Attempt At",
    "Completed At",
]

LOG_HEADERS = [
    "Timestamp",
    "Brand",
    "Post ID",
    "Platform",
    "Action",
    "Result",
    "Details",
]


def brand_headers() -> list[str]:
    return [*BRAND_USER_COLUMNS, *BRAND_TOOL_COLUMNS]


# Zero-based indices into `brand_headers()`, for writes and API requests.
COL_ID = BRAND_USER_COLUMNS.index("ID")
COL_TYPE = BRAND_USER_COLUMNS.index("Type")
COL_ACTION = BRAND_USER_COLUMNS.index("Action")
COL_TOOL_START = len(BRAND_USER_COLUMNS)
COL_TOOL_END = COL_TOOL_START + len(BRAND_TOOL_COLUMNS)

# Dropdowns. `Platforms` is deliberately free text: it is a comma-separated
# subset (e.g. "FB, IG") and a one-of list cannot express that. `sync`
# validates it instead, which also lets it say *why* a value is wrong.
TYPE_CHOICES = [t.value for t in PostType]
ACTION_CHOICES = ["", *[a.value for a in HumanAction]]
ACTIVE_CHOICES = ["TRUE", "FALSE"]

# Rows of validation/formatting to apply below the header.
MAX_DATA_ROWS = 2000


def a1_column(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def header_index(headers: list[str], name: str) -> int | None:
    """Find a column by name, tolerating stray whitespace and case drift."""
    wanted = name.strip().casefold()
    for i, header in enumerate(headers):
        if header.strip().casefold() == wanted:
            return i
    return None
