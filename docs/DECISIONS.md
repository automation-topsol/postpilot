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

---

## Phase 3 — the state machine

### Leases expire before reconciliation runs, not during selection

The first implementation expired stale leases while selecting what was due,
which is step 5 — but reconciliation is step 4. A run that died holding a lease
therefore became `unknown` only *after* the reconciler had already walked past,
so it sat untouched until the run after next. Moving expiry to its own step,
ahead of reconciliation, means a crashed run is settled on the very next run.
Found by a failing test rather than by reading the code, which is the whole
reason the failure-injection suite exists.

### Each row is leased immediately before its own API call

Leasing every due row in one batch up front is cheaper — one `_State` write per
run instead of one per row — and it is what the first version did. The problem
shows up when a run dies partway through: every row it never reached is sitting
`publishing`, and each one then needs a 20-minute lease expiry plus a
reconciliation lookup to establish that nothing happened. Leasing per row means
an unattempted row is simply still `scheduled`, needing nothing. At 20-50 posts
a week the extra writes are irrelevant; the cleaner failure semantics are not.

### A negative lookup is evidence; a failed lookup is not

This is the distinction the whole reconciler turns on, and it is what lets
`unknown` ever resolve to anything other than "ask a human". If `find_recent`
returns successfully and our post is not in it, the post is not there — so
rescheduling cannot duplicate anything. If `find_recent` raises, we have
learned nothing at all, and the state stays `unknown`. The guarantee says
PostPilot stops when a result is *ambiguous*; a successful negative result is
not ambiguous, and treating it as such would strand every post that ever hit a
timeout.

### Matching needs caption and time together

Caption alone would match a post from last month with the same words — brands
do repeat copy. Time alone would match whatever else happened to go out in the
same half hour. Requiring both, with a ±30 minute window around the recorded
attempt, is tight enough to be trustworthy and loose enough to survive the
platforms reformatting captions, which they do (whitespace, trailing hashtags).
Comparison is on a normalised 60-character prefix for the same reason.

### When the Sheet fails mid-run, the lease is the recovery

There is a real temptation to catch the write failure and mark the row
`unknown` — except the thing that just failed is the only place we could write
that. So the run is allowed to die. What saves it is the ordering: the lease
was written *before* the API call, so `_State` already says `publishing`, and
that row ages into `unknown` and gets reconciled next run. This is the single
clearest argument for writing the lease first, and two tests now assert the
full journey rather than just the first half.

### An adapter that raises is `unknown`, never a retry

Adapters return a classified `PublishResult` rather than raising. If one raises
anyway — a bug, a library changing its exception type — the runner cannot know
whether the request went out. `UNKNOWN` is the only safe reading: being wrong
that way costs a human a glance, while the alternative publishes twice and v1
cannot delete a post.

### Media failures are permanent rather than retryable

A missing Drive file, an ambiguous name, a video over the duration limit: none
of these get better by waiting, and all of them need a person. Classifying them
as retryable would burn all three attempts over an hour and then fail anyway,
with the teammate seeing nothing useful until the end. Permanent puts the real
message in the `Error` column immediately, and fixing the row re-opens it
automatically via the content hash.

### `--dry-run` runs everything except the lease

It syncs, resolves Drive, normalises media, uploads to R2 and works out exactly
what each API call would carry — then stops. Because no lease is written, a dry
run is safe to execute at any moment, including while the real scheduler is
running, and it cannot affect what the next real run does. Verified against the
live Sheet: after a dry run, `_State` still read `attempts=0, attempt_id=""`.

---

## Phase 4 — Facebook

### Classification lives in the HTTP layer, not in each adapter

Whether a failure can be retried is the single most consequential judgement the
tool makes, so it is made in one place. `publishers/http.call()` maps httpx's
own distinction onto ours: `ConnectError`/`ConnectTimeout` mean the connection
never opened, so nothing was sent and a retry is safe; `ReadTimeout` and
`RemoteProtocolError` mean the request went out and the answer did not come
back, which is `unknown`. Adapters that called httpx directly would each have
to re-derive this, and one of them would eventually get it wrong.

### Reading the Page uses `/published_posts`

The obvious edge is `/{page}/feed`, and it is what the brief implies. Against
the real API it returns `(#10) This endpoint requires the
'pages_read_engagement' permission or the 'Page Public Content Access' feature`
even with a token that *has* `pages_read_engagement` — because `/feed` includes
posts by other people on the Page, which needs the extra App feature.
`/published_posts` needs only what we have, and is narrower in exactly the
right way: reconciliation wants posts this Page published, and a visitor post
that happened to quote our caption would be a false match.

### `find_recent` raises instead of returning an empty list

An empty list and a failed lookup are indistinguishable to the caller unless
the failure is loud, and the reconciler treats them completely differently:
empty means "not published, safe to reschedule", failed means "we learned
nothing, ask a human". Returning `[]` on an API error would silently convert
the second into the first, which is the one mistake that ends in a double post.

### A half-created carousel is `unknown`

Carousel children are real objects on the Page from the moment they are
created, even with `published=false`. So the failure windows are not equal: a
failure on the first child means nothing exists and a retry is clean, while a
failure on the second child, or between the children and the feed post, leaves
photos behind that a retry would duplicate. Those cases return `unknown` with a
count of what is orphaned, so the human knows what to clear before using
`Action=retry`.

### `post_id` is recorded, `id` is discarded

`/photos` returns both and they are different numbers — Phase 0 saw
`122114927445466419` and `1355072654348977_122114927469466419` for one post.
`id` identifies the photo object; `post_id` identifies the feed post, and it is
what produces a working permalink and what `/published_posts` returns during
reconciliation. Recording the wrong one gives the teammate a link that 404s and
quietly breaks `unknown` resolution.

---

## Phase 5 — Instagram

### `media_publish` has no idempotency key, and the design follows from that

Facebook's `/photos` is a single call whose failure modes map cleanly onto our
classification. Instagram's publish is the second half of a container flow, and
the API offers nothing to make it idempotent — no client token, no dedupe
window. If the request leaves and the answer does not come back, the only way
to find out what happened is to look. So every non-4xx failure at that step is
`unknown` and carries the container ID, which reconciliation uses to ask. A 4xx
is different: "already published" or "invalid creation_id" are definitive
answers, and treating them as ambiguous would strand posts that are simply
finished.

### A failed carousel child is permanent on Instagram but unknown on Facebook

These look inconsistent side by side and are not. A Facebook carousel child is
a real photo object attached to the Page the moment it is created, so abandoning
one leaves debris a retry would duplicate. An Instagram container is invisible
to everybody until `media_publish`, and expires by itself after 24 hours, so
abandoning one costs nothing and a retry is genuinely clean. The classification
should follow what the platform leaves behind, not what the code looks like.

### The quota is checked before attempting, not after failing

Instagram allows 100 published posts per rolling 24 hours (50 for carousels).
Discovering that by being rejected costs one of the post's three attempts and
puts a confusing error in front of the teammate. Reading
`content_publishing_limit` first costs one cheap GET and turns a wasted attempt
into a clear "waiting for the limit to reset". If the quota endpoint itself
cannot be read, publishing proceeds anyway: a diagnostic that fails must not
become a gate.

### Polling sleeps through an injected function

`InstagramPublisher` takes `sleep` as a constructor argument. The container poll
genuinely needs to wait five seconds between checks in production, and genuinely
must not in tests — and reaching for `monkeypatch` on `time.sleep` from a dozen
tests is both noisier and easier to get wrong than passing a no-op in. The
timeout is injected for the same reason, which is how the "still processing
after the deadline" branch is tested at all.

---

## Phase 6 — LinkedIn, written but unverified

### Shipping an unverified adapter, gated three ways

The alternative was to leave LinkedIn unwritten until access arrives, and to
lose the context in which the rest of the system was built. Writing it now costs
little and keeps the shape of the thing intact, but an adapter that has never
run is a genuine liability if it can be reached by accident. So it is gated:
the registry adds it only when `LINKEDIN_ACCESS_TOKEN` exists, `doctor` warns
on every run that it is unverified, and the fixtures carry a comment saying
they are documentation-derived rather than recorded. Honest labelling is what
makes the code an asset rather than a trap.

### LinkedIn uploads bytes; Meta fetches URLs

Meta takes an `image_url` and fetches the media from its own servers, which is
the entire reason R2 has to be publicly readable. LinkedIn does the opposite:
it issues an upload URL and expects the bytes. So the LinkedIn adapter has to
download our own R2 object and PUT it back up — a round trip that does not exist
for the other platforms. `fetch` is a constructor argument both to make that
testable and to make the asymmetry visible to whoever reads the class next. A
failure fetching our own media is `retryable`, not `permanent`: R2 being briefly
unreachable says nothing about the post.

### A video part with no ETag is `unknown`

`finalizeUpload` requires the ETag of every uploaded part, in order. If a PUT
succeeds but the response carries no ETag, the upload cannot be completed and
we also cannot say what state the video is in on LinkedIn's side. Treating it as
a clean failure would risk a second upload of the same video; `unknown` sends it
to a human, which is the right cost for an ambiguous case.

### A refresh that returns no refresh token keeps the old one

LinkedIn's refresh response does not always include a new refresh token, and the
old one remains valid when it does not. Overwriting the stored value with an
empty string would lock the operator out at the next expiry, with no way back
except a full browser round trip — and they would only discover it sixty days
later, when the scheduler quietly stopped. Carrying the old value forward is one
line and removes the whole failure mode.

---

## Phase 7 — automation

### The digest falls back to `_Log` rather than being skipped

Telegram is optional, and the naive reading of "optional" is "skip it when it is
missing". That is wrong here, because the digest is the only routine evidence
that the scheduler is still running. Skipping it means the silence that follows
a dead cron looks exactly like the silence that follows a quiet week. Writing
the same text to `_Log` costs one batched append, puts it where the teammate
already looks, and keeps the signal honest. For the same reason `send()` never
raises: a notifier that crashes the run converts a reporting problem into a
delivery problem, which is strictly worse than the thing it was reporting.

### `cancel-in-progress: false`

GitHub's default for a concurrency group is to cancel the older run, which is
right for CI and wrong here. The window between a platform returning 2xx and our
`_State` write is the single most dangerous moment in the system — a run
cancelled there leaves a published post recorded as `publishing`, and although
the lease and reconciliation would eventually settle it, deliberately creating
that situation on every overlap is indefensible. A second run queueing behind
the first costs nothing, because the second will find the rows leased and skip
them.

### The cron is staggered off the hour

`7,22,37,52` rather than `0,15,30,45`. GitHub's scheduled-workflow queue is
heavily loaded on the hour and runs are routinely delayed by several minutes
there. The promised SLA is 15-30 minutes end to end, which leaves no room to
donate five of them to queueing. The launchd script uses the same minutes, so
switching between the two changes nothing about when posts go out.

### The failure artifact is safe because redaction happens at the source

Uploading a run log on failure is only acceptable if the log cannot contain a
token, and the way to guarantee that is to redact in the logging formatter
rather than when the artifact is assembled. Every line has already been through
`postpilot.logging.redact` and the env-value sweep before it reaches stdout, so
`tee run.log` captures redacted text and there is no second place for the rule
to be forgotten.

### Adding a brand touches three places, and the docs say so

A new brand needs a `_Brands` row, a `META_PAGE_TOKEN_<SLUG>` repository
secret, and that same variable added to the `env:` block of both workflows.
GitHub Actions cannot enumerate secrets — each one must be named in the
workflow — so this cannot be automated away. The honest response is to document
it prominently rather than to let someone discover it when a new brand silently
publishes nothing.

---

## The adversarial pass — what the bugs had in common

### "Not found" is only evidence when you actually looked

Two of the seven defects were the same mistake wearing different clothes. A
`--brand`-scoped run reconciled other brands' `unknown` rows with no captions
loaded, and caption matching compared truncated prefixes. In both cases the
reconciler received a negative answer that was not evidence of anything, and
treated it as proof the post had never been published. The guarantee survives
only because a *successful* negative lookup is genuine evidence of absence —
so every path that can produce a fake negative is a path to a double publish.
The fix in both cases was to make the scope explicit rather than implicit:
`captions` is now the definitive list of what a run loaded, and matching
requires whole-prefix containment with a minimum length rather than a
truncated comparison.

### A row copied is a row duplicated

The tool assigns `ID` and treats it as immutable, which is right — but nothing
stopped two rows carrying the same one, and copying a row to make a similar
post is the single most natural thing a non-technical person does in a
spreadsheet. The two rows then shared `_State`, so publishing one marked the
other published and it silently never went out. Neither copy can be presumed
the original, so both are stopped, and the message names the one-keystroke fix:
clear the ID on the copy and it gets a new one.

### EXIF is not metadata you can ignore

Phone cameras do not rotate pixels; they record an orientation tag and leave
the buffer as shot. Pillow does not apply it, and JPEG re-encoding discards it,
so a portrait photo silently published sideways — and because the aspect-ratio
logic ran on the un-rotated dimensions, it padded the wrong axis too, making it
worse. One call to `ImageOps.exif_transpose` at the top of `_flatten` fixes
both. Worth recording because the bug is invisible in code review and obvious
to anyone looking at the post.

### A status that understates is worse than one that alarms

`published` on one platform plus `invalid` on another rolled up to
`scheduled` — the one word that says "nothing needs you". The roll-up now
distinguishes *pending* (queued, fine) from *blocked* (cannot go as things
stand), and only the latter, alongside something already live, produces
`partial`. A row still waiting on a platform that has not run yet is
deliberately NOT `partial`, because `partial` should mean "look at me".

### A read-only-sounding flag that mutates is a trap

`--skip-media` reads like a way to inspect without touching Drive. It also
changes what the content hash is computed from, so running it once re-opened
every failed and invalid row and reset their attempts. Rather than making the
hash mode-independent — which would mean either ignoring Drive changes or
ignoring the Media text, both wrong — the flag now requires `--dry-run`. The
capability is preserved, the footgun is not.

### Silence is not success

`--post <typo>` printed "nothing to do" and exited 0. For a command someone
runs to publish one specific post, that is the worst possible response: it
looks exactly like the post going out. Naming a post that does not exist is now
an error, checked before anything is leased.
