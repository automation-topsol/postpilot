# Design decisions

One entry per decision, each explaining **why** — so a future session (or a
future me) can tell a deliberate choice from an accident, and knows what would
have to change for the choice to be revisited.

Newest decisions go at the bottom of their section.

---

## Phase 0 — access spike

### Pinned API versions: Graph `v25.0`, LinkedIn `202609`

Both platforms version their APIs and deprecate on a fixed calendar, so an
unpinned client is a client that breaks on someone else's schedule. Graph
**v25.0** (released 2026-02-18) rather than the newest v26.0 (2026-07-29):
v25.0 is supported until 2028-07-29, which is 22 months of runway, and has had
seven months of production soak. Chasing the newest version buys a few months
of extra runway in exchange for being the one who finds its bugs — a bad trade
for an unattended scheduler that nobody watches. LinkedIn **`202609`** is the
current default moniker; note that `202510` sunsets on **2026-10-15**, so
anything written against it must move before then. Both constants live in one
place (`postpilot/apis.py` from Phase 1; the spike hardcodes the same value so
what we prove is what we ship).

### Media limits verified against live docs, not the brief

`CLAUDE_CODE_PROMPT.md` §5 supplied starting figures and explicitly said to
verify them. Four of them were wrong — Facebook images allow **10 MB**, not
4 MB; Instagram reels accept **0.01:1–10:1** and **23–60 fps**, not a strict
9:16/30 fps; LinkedIn images are capped by **pixel count** (36,152,320 px), not
a byte size. Two of those would have caused us to reject media the platform
would have accepted, and the LinkedIn one is the wrong *kind* of limit
entirely, so a byte-based check would have passed files that fail and failed
files that pass. Everything now lives in `docs/MEDIA_POLICIES.md` with the
source URL and verification date next to it, and the corrections are listed
there in §6 so nobody "fixes" the corrected values back. We still normalise IG
reels to 9:16/30 fps — that is what the platform favours in practice — but it
is now recorded as *our* choice rather than a platform rule.

### A missing credential is a SKIP, not a crash

The operator fills `.env` incrementally, across days, while waiting on app
reviews and DNS. A spike that dies on the first unset variable would tell them
nothing about the other four services. Every check therefore reports
PASS/FAIL/SKIP/WARN independently, each script runs in its own subprocess under
`run_all.py`, and any exception inside a check becomes a FAIL for that check
alone. The exit code reflects only genuine failures, so SKIP-heavy early runs
stay green and the signal doesn't get lost in noise.

### The R2 check tests an *anonymous* GET over the custom domain

This is the only check whose failure mode is invisible until publish time.
Meta and LinkedIn fetch media **from their own servers**, not from us, so a
bucket we can authenticate to and write to can still be completely unusable.
Testing our own authenticated `GET` would prove nothing. `check_r2.py`
therefore does a credential-free `httpx.get` against
`R2_PUBLIC_BASE_URL` and compares the bytes — exactly what Meta's fetcher does.
It also flags `*.r2.dev` URLs, which are rate-limited and documented as unfit
for production, and fails any non-HTTPS base URL.

### `boto3` checksum calculation set to `when_required`

From 1.36, boto3 sends `x-amz-checksum-crc32` on every `PUT` by default. R2's
S3-compatible API rejects it, producing a failure that looks like a credential
problem and isn't. Setting `request_checksum_calculation="when_required"` (with
the matching response setting) disables the default without giving up checksums
where they are genuinely needed. Encoded in the spike so Phase 2's `R2Store`
inherits the fix rather than rediscovering it.

### The Sheet write test uses a scratch tab

Proving write access means writing something. Writing into the operator's own
tabs risks their content, and writing into a corner of a real tab leaves
litter. `check_google.py` creates a `_spike_probe` worksheet, writes two cells,
reads them back, and deletes it in a `finally` — so the probe is cleaned up
even when the read-back fails. It also confirms Drive returns `md5Checksum` for
every non-Google-native file, because the deterministic R2 key
(`…-{drive_md5}.{ext}`) is unusable without it, and a Drive file without an md5
would silently break media reuse.

### The IG User ID is cross-checked against the Sheet

`check_meta.py` reads `instagram_business_account` from the Page and compares
it with the ID in `_Brands`. A mismatch is a FAIL rather than a warning: the
failure it prevents is publishing a brand's content to a different brand's
Instagram account, which is unrecoverable (v1 has no post deletion) and is
exactly the kind of error a copy-pasted ID produces.

### `post_id`, not `id`, is recorded for Facebook photos

`POST /{page}/photos` returns both. `id` identifies the photo object; `post_id`
identifies the feed post. Recording the wrong one gives a `Remote URL` that
404s for the human checking it, and breaks reconciliation matching. Noted here
because the two are trivially confusable and the response gives no hint.

---

## Amendments to the original brief

### One LinkedIn token for all brands

The brief specified `LINKEDIN_ACCESS_TOKEN_<SLUG>` per brand, by symmetry with
Meta. That symmetry is false. A Meta Page token really is scoped to one Page,
so it must be per brand. A LinkedIn member token, by contrast, acts on behalf
of a *person*, and authorises every Company Page that person administers — so
per-brand LinkedIn tokens would be N copies of the same string, N secrets to
rotate on every 60-day expiry, and N chances for one copy to go stale and fail
a single brand mysteriously. The secrets are therefore flat and unsuffixed
(`LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_REFRESH_TOKEN`, `LINKEDIN_TOKEN_EXPIRES`),
and the only per-brand LinkedIn value is the **org URN**, which lives in
`_Brands` alongside the other per-brand IDs. Consequently `postpilot auth
linkedin` takes no `--brand`, unlike `auth meta`.

### LinkedIn deferred to Phase 6; Facebook and Instagram built first

LinkedIn Community Management API access is pending approval, and approval is
not on our schedule. Blocking on it would idle the whole project. The brief
already anticipated this ("if not approved, tell me and we build FB/IG first").
The important consequence is that everything *except* the adapter is still
built as though LinkedIn exists — `LI` is a valid `Platforms` value, the
`_Brands` tab has its org URN column, `_State` treats LI as a peer platform,
and the secrets are documented. Turning LinkedIn on later is writing one
adapter against an existing interface, not a refactor. Its media limits are
already recorded in `docs/MEDIA_POLICIES.md` §4 so Phase 6 needs no new
research pass.

### Local git only, no remote yet

`gh` is not installed and the GitHub repo does not exist yet. The Actions
workflows are still written and committed in Phase 7 — they cost nothing while
dormant — but nothing in the tool may *assume* a remote. This is also why the
keep-alive workflow matters later: GitHub disables scheduled workflows after 60
days of repository inactivity, and a scheduler that silently stops scheduling
is the worst failure this project has.

---

## Standing decisions inherited from the brief

Recorded here because the reasoning matters when someone is tempted to change
them.

### `_State` is the source of truth, not the brand tab

The brand tab's `Status` is a roll-up written *for humans*. Humans reorder
rows, insert rows, paste over cells and undo things. If the code read its own
state back from a surface a human edits freely, a stray paste could resurrect a
published post. `_State` is hidden, tool-owned, keyed by `(Post ID, Platform)`,
and never read by a person — which is what makes the delivery guarantee
enforceable.

### The lease is written before the API call, never after

Writing `publishing` + a fresh `Attempt ID` *before* any request means a
crashed run leaves evidence. The alternative — recording only outcomes — cannot
distinguish "never sent" from "sent, then we died", and the only safe response
to that ambiguity is to never retry anything, which would make the scheduler
useless. The 20-minute lease window is what lets two concurrent workflow runs
coexist: younger than 20 minutes means another run owns it, older means that
run died and the row becomes `unknown` rather than being blindly retried.

### Ambiguity resolves to a human, never to a retry

Every classification boundary in §4 of the brief leans the same way: if we
cannot prove the request was *not* delivered, we do not send it again. A
duplicate post is visible to the brand's audience and cannot be undone by the
tool (v1 has no deletion). A post that waits for a human is merely late, and
lateness is already budgeted for by the 15–30 minute SLA. The asymmetry in
those costs is the whole reason `unknown` exists as a distinct state rather
than being folded into `retryable_failed`.

### No local media cache

The R2 key is `{brand}/{post_id}/{platform}/{policy_version}-{drive_md5}.{ext}`
— derived entirely from immutable inputs. A `HEAD` against that key answers
"already prepared?" without any local state, which means a GitHub Actions
runner (fresh filesystem every run) behaves identically to a laptop. A cache
file would have to be committed, synced, or rebuilt, and all three are worse
than one `HEAD` request. `POLICY_VERSION` is in the key so that changing a
normalisation rule invalidates old output rather than silently reusing it.
