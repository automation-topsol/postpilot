"""Which adapters exist right now.

Deliberately explicit rather than auto-discovered: a platform quietly having no
adapter must read as "Phase N has not happened yet", not as a mysterious
absence. The run algorithm turns a missing adapter into a clear permanent
failure on that platform alone, leaving the others to publish.
"""

from __future__ import annotations

import os

import httpx

from postpilot.models import Platform
from postpilot.publishers.base import Publisher
from postpilot.publishers.facebook import FacebookPublisher
from postpilot.publishers.http import build_client
from postpilot.publishers.instagram import InstagramPublisher
from postpilot.publishers.linkedin import LinkedInPublisher


def available_publishers(client: httpx.Client | None = None) -> dict[Platform, Publisher]:
    """Built adapters, keyed by platform.

    One shared httpx client, so connections are reused across every post in a
    run. LinkedIn arrives in Phase 6, once API access is approved.
    """
    shared = client or build_client()
    publishers: dict[Platform, Publisher] = {
        Platform.FB: FacebookPublisher(shared),
        Platform.IG: InstagramPublisher(shared),
    }

    # LinkedIn is registered only when a token exists. The adapter has never
    # run against the live API (access was pending when it was written), so
    # gating it on the token means it cannot be reached by accident — and when
    # a token does appear, that is a deliberate act by the operator.
    if os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip():
        publishers[Platform.LI] = LinkedInPublisher(shared)

    return publishers
