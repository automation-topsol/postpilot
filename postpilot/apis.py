"""Pinned external API versions — the ONLY place these are written down.

Both platforms deprecate on a fixed calendar, so an unpinned client breaks on
someone else's schedule. The rationale for each choice is in
`docs/DECISIONS.md`; the verified media limits that go with them are in
`docs/MEDIA_POLICIES.md`.
"""

from __future__ import annotations

# Released 2026-02-18, supported until 2028-07-29. Deliberately not the newest
# (v26.0): 22 months of runway plus seven months of production soak beats a few
# extra months of runway on a version whose bugs nobody has found yet.
GRAPH_API_VERSION = "v25.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Meta's resumable upload host for video/reels — a different host, not a path.
GRAPH_UPLOAD_BASE = "https://rupload.facebook.com"

# LinkedIn's `Linkedin-Version` header, YYYYMM. Version 202510 sunsets
# 2026-10-15. Unused until Phase 6, pinned now so it is not forgotten.
LINKEDIN_VERSION = "202609"
LINKEDIN_API_BASE = "https://api.linkedin.com/rest"
LINKEDIN_RESTLI_VERSION = "2.0.0"

# Bumped whenever a media normalisation rule changes, so previously transformed
# files are never silently reused. Part of every R2 key.
POLICY_VERSION = "v1"

# Bumped whenever the *content hash inputs* change. Phase 1 hashes the raw
# Media cell text; Phase 2 replaces that with resolved Drive file IDs + md5s
# and must bump this to h2. A hash change only ever re-validates a row — a
# published platform is never republished, it only gets a Notes warning.
HASH_VERSION = "h1"
