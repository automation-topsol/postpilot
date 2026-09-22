"""Which adapters exist right now.

Deliberately explicit rather than auto-discovered: a platform quietly having no
adapter must read as "Phase N has not happened yet", not as a mysterious
absence. The run algorithm turns a missing adapter into a clear permanent
failure on that platform alone, leaving the others to publish.
"""

from __future__ import annotations

from postpilot.models import Platform
from postpilot.publishers.base import Publisher


def available_publishers() -> dict[Platform, Publisher]:
    """Built adapters, keyed by platform.

    Phase 4 adds Facebook, Phase 5 Instagram, Phase 6 LinkedIn.
    """
    publishers: dict[Platform, Publisher] = {}
    return publishers
