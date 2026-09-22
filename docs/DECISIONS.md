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

### Telegram is optional, with a `_Log` fallback rather than a silent gap

Telegram is not set up, and making the daily summary depend on it would mean
the tool reports nothing until a bot exists. But simply skipping the summary
when unconfigured is worse than it looks: the summary is the *only* routine
signal that the scheduler is alive, and a scheduler that silently stops is this
project's worst failure. So a missing notifier is a `doctor` **warning**, never
an error, and `postpilot summary` writes the same digest to the `_Log` tab
instead — where it is durable, timestamped and visible to the non-technical
teammate who already lives in the Sheet. Publishing never depends on the
notifier in either direction; the notifier only ever reads state that already
exists.

### R2 stays on the `r2.dev` URL, and that is recorded as a choice

No custom domain is configured, so `R2_PUBLIC_BASE_URL` is the `*.r2.dev`
development URL. Cloudflare rate-limits r2.dev and documents it as unsuitable
for production, which is a real constraint — but the relevant number is our
volume: 20–50 posts/week, each item fetched a handful of times by Meta's
fetcher. That is orders of magnitude below where the limit bites. The spike
therefore reports r2.dev as **PASS**, not a warning, because a warning that
fires on every run for a deliberate choice trains people to ignore warnings.
The `MediaStore` interface exists precisely so that swapping in a custom domain
later is a change to one environment variable and nothing else.

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


---

## Phase 0 — what the spike found, and what it changed

### A short-lived Meta token is a FAIL, not a warning

The Page token in `.env` was short-lived and had already expired. It would have
been easy to report "expires soon" and move on, but the distinction that
matters is not *when* it expires — it is *what kind of token it is*. A
correctly-minted long-lived Page token reports **no expiry at all**; anything
with an expiry timestamp came from the Graph API Explorer and will die in hours
regardless of how often it is refreshed. For an unattended scheduler that is a
hard blocker, so the check fails below 48 hours and prints the exact two-step
exchange (`fb_exchange_token` → `/me/accounts`) rather than a generic "token
expired". The same logic becomes `postpilot auth meta` in Phase 4.

### `AccessDenied` on PUT is reported as a token-scope problem, not a credential problem

`HeadBucket` succeeding while `PutObject` returns `AccessDenied` is a precise
signature: the keys are valid, the bucket name is right, and the token is
read-only. Reporting the raw botocore error would send the operator to re-check
credentials that are fine. The check now names the actual fix — create a token
with **Object Read & Write** — and separately downgrades a lifecycle-rule
`AccessDenied` to a warning, because reading lifecycle config needs
bucket-level permission that an object-scoped token legitimately lacks. Those
two failures have the same error code and completely different remedies.

### When the write fails, the public-read check reports SKIP rather than disappearing

The anonymous public GET is the most valuable check in Phase 0 — it is the only
one whose failure is invisible until a post is due. If the upload fails, that
check cannot run, and a silently absent check reads as a passing one. It now
emits an explicit `SKIP` saying it is *still unverified*, so the gap is visible
in the report rather than inferred from its absence.

### `tasks` is not available on `/me` for a Page token

Requesting it returns `(#100) Tried accessing nonexisting field (tasks)` — the
field only exists on `/me/accounts`, which needs a user token. The check now
probes for it quietly and falls back to `id,name,link`, reporting a SKIP that
explains publishing capability is implied by the `pages_manage_posts` scope
instead. Worth recording because the error message blames the field rather than
the token type, which sends you looking in the wrong place.

### rich markup is disabled in the spike's console

`Console()` interprets `[...]` as markup, which silently ate the brand slug in
the report title (`=== Meta [grandinvitation] ===` rendered as `=== Meta ===`)
and would have eaten any Meta error body containing brackets. Diagnostic output
that quietly drops content is worse than no output, so the console is
constructed with `markup=False, highlight=False`. Related: `Report.add` now
takes `_name`/`_detail` so an API field called `name` cannot collide with the
method signature — that collision was a `TypeError` raised in the middle of
reporting a successful check.

### `instagram_basic` turned out to already be granted

Recorded because the project brief carried the opposite assumption. The app has
both `instagram_basic` and `instagram_content_publish`, and @grand.invitation
resolves with a readable publishing quota. No workaround was needed, and none
should be built. The IG User ID (`17841432916654917`) goes into `_Brands`,
where `check_meta.py --ig-id` cross-checks it on every subsequent run.

### The end-to-end test published to both platforms, not just one

A single-platform test would have proven less than it appears: Facebook accepts
a `url` parameter and publishes synchronously, while Instagram requires the
two-step container flow with polling. They share almost no code path. Running
both against the same source image also surfaced the thing a one-platform test
hides — that the *same* Drive file needs *different* normalised output per
platform, stored under different R2 keys, which is precisely why `{platform}`
is in the key template rather than just `{brand}/{post_id}`.

### The dry run uploading to R2 is a feature, not a leak

`--dry-run` deliberately performs the full media pipeline including the R2
`PUT`, stopping only before the publish call and the lease write. This felt
wrong at first — a dry run that writes something — but it is what makes the
subsequent live run exercise the *reuse* path rather than the upload path, and
reuse is the behaviour that keeps R2 inside the free tier. It also means a dry
run surfaces media problems (wrong aspect, oversized, unfetchable URL) at the
time someone is actually looking at the output. The objects are addressed by
content hash and expire on their own, so an unused upload costs nothing.

---

## Phase 1 — models and the Sheet

### `_State` is rewritten wholesale, brand tabs are patched in place

Two different tabs, two different write strategies. `_State` is machine-owned
and no human edits it, so rewriting it entirely in one `update` is simplest and
cannot drift. Brand tabs are the opposite: a human may be typing in one while
the run is happening, so only the six tool-owned columns are touched, as a
single contiguous `M:R` range per row. That is one write per row rather than
six, which is what keeps the run inside the Sheets quota — the test
`test_tool_columns_are_written_as_one_contiguous_range` exists to stop anyone
"simplifying" it back into per-cell writes.

### Post IDs carry a brand prefix, and prefix collisions are a hard error

`gi-0042` is more useful than `0042` the moment two brands appear in `_Log`
together. The prefix comes from the brand name's initials, which is stable and
human-recognisable. The risk is two brands deriving the same prefix — "Grand
Invitation" and "Global Imports" both give `gi` — which would silently merge
their ID sequences and, worse, their `_State` rows. `parse_brands` therefore
detects prefix collisions at load time and reports them as a problem rather
than letting the ambiguity through.

### Config problems and incomplete configuration are separated

`restocklypos` is LinkedIn-only while LinkedIn access is pending, so it
legitimately has an enabled platform with no org URN. Treating that as an error
would make every single run of a correctly-configured Sheet exit non-zero,
which trains people to ignore the exit code. It is now a *warning*: reported
every run, never fatal. Structural faults — duplicate slugs, malformed slugs,
colliding prefixes — remain *problems* and do fail the command.

### But a row that cannot publish is still marked `invalid`

The counterpart to the above, and a correction to an earlier note in CLAUDE.md.
When a platform has no target ID, rows targeting it are `invalid`, not
`scheduled`. A row that can never publish must not claim to be waiting — that
would hide the gap behind a reassuring status. What the earlier note was right
about is the *risk*: the teammate must not be sent to fix a row that is already
correct. That is solved in the message, which says "brand setting: … (nothing
wrong with this row)", not by mislabelling the state.

### Sheet protection is warn-only

The Sheets API enforces hard protection through an editor allow-list. Getting
that list wrong with a service account is a real way to lock the Sheet's human
owner out of columns on their own document, and unpicking it needs the API
again. The actual risk being managed is an accidental paste or drag-fill into
the tool-owned columns, and a warning dialog stops that just as well. The
gentler control has a far better worst case.

### Ambiguous dates are read day-first and say so

`03/04/2026` is 3 April in Pakistan and 4 March in the US, and the Sheet's
display string depends on the viewer's locale, so the tool cannot know which
was meant. Rejecting it outright would block legitimate use; guessing silently
could publish a wedding invitation a month early. So it guesses — day-first,
matching the operator's locale — and writes what it assumed into the row's
`Notes`, turning a silent assumption into a visible one. `YYYY-MM-DD` is
unambiguous and is what the non-technical guide tells people to use.

### Sync is deliberately read-mostly and never publishes

`sync` validates, assigns IDs and reconciles `_State`, but never calls a
publishing API and never writes a lease. That separation is what lets `status`
reuse it verbatim with `write=False` to show the truth without changing it, and
what makes the whole validation layer testable against a fake client with no
network at all. `publish` in Phase 3 consumes what `sync` produces rather than
recomputing it.

### `parse_state` drops corrupt rows instead of raising

`_State` is machine-written, so a malformed row means corruption rather than
user error. Aborting the run would leave every *other* post unpublished because
of one bad row — a far worse outcome than treating that one (post, platform) as
unknown-to-us and letting `sync` recreate it as `scheduled`. The lease and the
hash protect against the duplicate-publish risk that recreation would otherwise
carry.

---

## Phase 2 — Drive, media and R2

### Padding beats cropping, and refusing beats trimming

Two different failure modes, two different answers. For aspect ratios, the
source is a design someone laid out deliberately, so cropping can silently
remove the date, the venue or a logo; padding onto a blurred copy of the image
keeps everything visible at the cost of some soft bars. Cropping is used only
inside a 5% tolerance, where padding would waste more of the frame than
trimming removes. For *duration*, there is no equivalent compromise: a
four-minute video is not a ninety-second video with the end cut off, and
silently truncating one would publish something nobody approved. So duration
violations raise, with a message that reaches the teammate's `Error` column.

### Carousels take the median aspect ratio, not the first item's

Instagram requires every item in a carousel to share one ratio, so the ratio
has to be decided for the set rather than per item. Taking the first item's
would let whichever image happened to be typed first dictate the whole post —
and if that one is a panorama, every other item gets heavily padded. The median
is stable against a single outlier, which is the common case: four square
images and one wide one should produce a square carousel.

### Carousel keys carry an index

The R2 key is derived from the source file's Drive md5. Two *identical* files
in one carousel — which happens, deliberately, when someone repeats a frame —
share an md5 and would collapse onto the same key, silently turning a five-item
carousel into a four-item one. Appending the position makes each item
addressable. Single-item posts keep the unindexed key so the common case stays
readable.

### A video canvas is sized from the source's long edge

The obvious implementation sizes the output from whichever edge matches the
target orientation: for a 9:16 target, take the source height. That turns a
1920x1080 clip into a 608x1080 canvas — genuinely 9:16, above Facebook's
540x960 minimum, and visibly terrible, because the actual picture is scaled
down to 608 wide before the bars are added. Sizing from the source's long edge
gives 1080x1920 and keeps the detail. Found by running the pipeline and reading
the numbers, not by reading the code, which is why the test asserts the exact
output dimensions rather than just the ratio.

### ffmpeg inputs must be declared before output options

The silent-audio track for videos with no sound is a second `-i` input. Placed
after the encoding options, ffmpeg parses it as an option applied to the
*output* file and fails with "you are trying to apply an input option to an
output file or vice versa" — which reads like a filter problem and is not.
Inputs first, then filters, then codecs, then the output path. Recorded because
the error message actively misdirects.

### `prepare` checks every key before downloading anything

The HEAD-before-PUT check runs across *all* of a post's media first. If every
object is already present the post is finished without downloading a single
byte from Drive, which is the common case on re-runs and the reason the
deterministic key exists at all. Only when something is missing does it fall
through to downloading — and for carousels it downloads everything at that
point anyway, because the shared aspect ratio cannot be computed without all
the dimensions.

### The content hash depends on Drive identity, not on typed text

Phase 1 hashed the raw Media cell because Drive resolution did not exist yet.
That had a real flaw: renaming a file in Drive changed nothing in the Sheet, so
a post could silently point at different bytes with an unchanged hash — and
conversely, tidying up a file name would re-open a post whose content had not
changed. Hashing `{file_id}:{md5}` fixes both directions: identity plus content,
never presentation. `HASH_VERSION` moved `h1` -> `h2` to make the change
deliberate; the bump re-opens failed and invalid rows, which is harmless, and
leaves published rows alone, which is the point.

### Drive resolution errors are row errors, not crashes

"No file named X", "ambiguous", "that is a Google Slides deck" and "no checksum"
all surface as validation issues on the row, in the `Error` column, phrased for
someone who has never seen a terminal. They arrive during `sync`, which means a
typo in a file name is caught when the row is written rather than when the post
is due. The alternative — discovering it at publish time — turns a typo into a
missed post.

### `doctor` warns for optional things and fails for required ones

A missing Telegram bot must not make a healthy install look broken, and neither
must a LinkedIn token while access is pending. Both warn. What fails is
anything that would actually stop a publish: a short-lived Meta token, an
unreadable Sheet, a Drive folder we cannot see, an R2 bucket the public cannot
read. Every failure names the fix rather than the symptom — `AccessDenied` on a
PUT reports "the R2 token is read-only", because the credentials are fine and
sending someone to re-check them wastes an hour, as it did during Phase 0.

### Escaping data before it reaches rich

`Console.print` interprets `[...]` as markup, so a finding named
`Drive [grandinvitation]` rendered as `Drive ` — the brand name vanished. The
same bug appeared in the Phase 0 spike and was fixed there by disabling markup
wholesale; here markup is genuinely wanted for colour, so every data-derived
string is passed through `rich.markup.escape` instead. Error text is the worst
offender because it routinely contains quoted file names and bracketed context.
Diagnostic output that silently drops content is worse than no output.
