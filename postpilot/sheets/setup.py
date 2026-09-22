"""`postpilot sheet init` — create or repair the tabs the tool expects.

Idempotent by design: it is the thing you run when something looks wrong, so
running it twice must be safe and running it on a healthy Sheet must be a
no-op. It never touches a cell of content — only headers, structure and
validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from postpilot.logging import get_logger
from postpilot.sheets.client import (
    SheetClient,
    brand_tab_requests,
    brands_tab_requests,
    log_tab_requests,
    state_tab_requests,
)
from postpilot.sheets.parse import parse_brands
from postpilot.sheets.schema import (
    BRANDS_HEADERS,
    BRANDS_TAB,
    LOG_HEADERS,
    LOG_TAB,
    STATE_HEADERS,
    STATE_TAB,
    brand_headers,
)

log = get_logger(__name__)


@dataclass
class InitReport:
    created: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.repaired)


def initialise(client: SheetClient, *, only_brand: str | None = None) -> InitReport:
    report = InitReport()
    requests: list[dict] = []

    # --- the three tool-owned tabs ----------------------------------------
    for title, headers, builder in (
        (BRANDS_TAB, BRANDS_HEADERS, brands_tab_requests),
        (STATE_TAB, STATE_HEADERS, state_tab_requests),
        (LOG_TAB, LOG_HEADERS, log_tab_requests),
    ):
        worksheet, created = client.ensure_tab(title, headers)
        (report.created if created else report.repaired).append(title)
        requests.extend(builder(worksheet.id))

    # --- one tab per brand -------------------------------------------------
    brands_tab = client.read(BRANDS_TAB, refresh=True)
    brands, problems, warnings = (
        parse_brands(brands_tab.headers, brands_tab.rows) if brands_tab else ([], [], [])
    )
    report.problems.extend(problems)
    report.notes.extend(warnings)

    if not brands:
        report.notes.append(
            f"{BRANDS_TAB} is empty — add a row per brand, then run `sheet init` again "
            "to create each brand's tab"
        )

    for brand in brands:
        if only_brand and brand.slug != only_brand:
            continue
        worksheet, created = client.ensure_tab(brand.slug, brand_headers())
        (report.created if created else report.repaired).append(brand.slug)
        requests.extend(brand_tab_requests(worksheet.id))

        # A brand with no reachable platform is legitimate (LinkedIn access is
        # still pending), but silently producing a tab nothing will ever
        # publish from is not — so say it once, here.
        if not brand.enabled_platforms:
            report.notes.append(f"{brand.slug}: no Enabled Platforms — its rows will never publish")

    if only_brand and not any(b.slug == only_brand for b in brands):
        report.problems.append(f"no brand with slug {only_brand!r} in {BRANDS_TAB}")

    client.apply_structure(requests)
    return report
