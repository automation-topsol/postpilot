# PostPilot — working agreement for Claude Code sessions

## START HERE (session handoff, last updated 2026-09-23)

**Everything is committed. Working tree clean. 316 tests pass, lint clean,
`postpilot doctor` reports 22 ok / 2 warnings / 0 failures.**

All seven phases are done. The two `doctor` warnings are expected states,
not problems, and share one cause — LinkedIn access is pending: no
`LINKEDIN_ACCESS_TOKEN`, and `restocklypos` has no org URN yet. Both clear
themselves when access lands. The daily summary goes by email (§0.5).

### The single most important open item

**`postpilot publish --live --confirm` has never actually published anything.**
The adapters are covered by `respx` fixtures and reconciliation is verified
against real data, but the full path — lease → prepare → adapter → record →
roll-up — has only ever run in `--dry-run`. Phase 0 proved the Graph calls, but
with the *spike's* code, not the adapter's.

Do it on a **text-only Facebook post first**: no media pipeline, no Instagram
container, smallest blast radius, and it still exercises the lease and the
state machine end to end.

```bash
# set a row's Date/Time to now, then:
uv run postpilot publish --live --confirm --post <id> --brand grandinvitation
```

### Waiting on the operator

1. Delete the two Phase 0 test posts (the tool cannot — v1 has no deletion).
2. Push to GitHub and add the secrets in the README; that is what actually
   turns the scheduler on. There is no remote yet and `gh` is not installed.
3. Telegram bot, if wanted. Until then the digest goes to `_Log`, by design.
4. LinkedIn Community Management API access.

### Things in the Sheet that are mine, not theirs

`_Brands` was seeded by me with both brands. `grandinvitation` has **three
sample rows** I added: one valid, one deliberately broken (a carousel with a
missing second file, which is why `status` shows 1 invalid), and one text-only.
Delete or repurpose them freely.

### Reading order for the rest of this file

§0 amendments (they override the original brief) · §3 the rules that keep the
delivery guarantee true · §6 the phase table · §10-17 what each phase found ·
**§18 the adversarial pass — seven real defects, two of which could publish
twice.** `docs/DECISIONS.md` has the *why* behind every non-obvious choice.

---

**Read this file before touching anything.** It is the condensed design contract.
The full, authoritative brief is `CLAUDE_CODE_PROMPT.md`; this file summarises it
and records the amendments agreed since. Where the two disagree, the amendments
in §0 win, then `CLAUDE_CODE_PROMPT.md`, then this summary.

---

## 0. Amendments to the original brief

These were agreed after `CLAUDE_CODE_PROMPT.md` was written and **override it**:

1. **One LinkedIn token for all brands.** The operator's single member token
   administers every Company Page. So the secrets are flat and unsuffixed —
   `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_REFRESH_TOKEN`, `LINKEDIN_TOKEN_EXPIRES` —
   *not* `…_<SLUG>`. This replaces the per-brand LinkedIn secrets in §7 of the
   brief. **Only the org URN is per brand**, and it lives in the `_Brands` tab.
   Meta tokens stay per brand (`META_PAGE_TOKEN_<SLUG>`), because a Page token
   really is page-specific.
2. **LinkedIn API access is pending.** Phase 0 covers Facebook, Instagram,
   Google (Sheets + Drive) and R2 only. LinkedIn media limits are recorded in
   `docs/MEDIA_POLICIES.md` from the published docs, but there is no LinkedIn
   live spike and no adapter until access is approved. Build FB/IG first.
   Everything else — models, `_Brands` columns, `Platforms` values, state
   machine, secrets layout — is written *as if* LinkedIn exists, so switching
   it on later is adding an adapter, not a refactor.
3. **Local git only for now.** No GitHub remote yet; `gh` is not installed.
   The Actions workflows still get written (Phase 7) and committed.
4. **Credentials live in `.env`**, filled in by the operator, never in chat and
   never committed. The Sheet ID, brand list and R2 base URL are read from
   `.env` / `config.yaml` — do not hardcode them anywhere.
5. **The daily summary goes by email; every notifier is optional.** Gmail
   SMTP with an app password (`SMTP_USER`, `SMTP_PASSWORD`, `SUMMARY_TO`, plus
   optional `SMTP_HOST`/`SMTP_PORT`/`SMTP_FROM`), to one or more addresses.
   Telegram remains as a second notifier behind the same interface
   (`postpilot/notify.py`). With **no** notifier configured, `doctor` gives a
   single **WARNING, never a failure**, and `postpilot summary` writes the
   digest to the **`_Log` tab** instead, so it is never silently lost. A
   missing notifier degrades reporting, not delivery — nothing about
   publishing may depend on it.
6. **No custom domain for R2.** `R2_PUBLIC_BASE_URL` is the `*.r2.dev`
   development URL, and that is the chosen configuration, not a temporary
   state to warn about. r2.dev is rate-limited and unsupported for production
   traffic, but at 20-50 posts/week that ceiling is nowhere near reached.
   Moving to a custom domain later is a one-line change to
   `R2_PUBLIC_BASE_URL` and nothing else - which is exactly why the
   `MediaStore` interface keeps the URL out of the rest of the code.

---

## 1. What this is

A CLI that reads a **Google Sheet**, pulls media from **Google Drive**,
normalises it, hosts it on **Cloudflare R2**, and publishes to **Facebook
Pages, Instagram Business accounts and LinkedIn Company Pages** for 4–6 brands
on a schedule. Driven by **GitHub Actions cron**. A non-technical teammate
operates it entirely from the Sheet — they never see a terminal.

**Volume:** 20–50 posts/week. **Timing SLA:** posts go out within 15–30 minutes
of the requested time; this is stated in the non-technical guide.

### Hard non-goals — do not add these
No web app. No database server. No Docker. No Celery, Redis or any queue. No
web UI, AI caption generation, analytics, stories, comment handling, post
editing/deleting after publish, or multi-user permissions. It stays
**CLI + Sheet + GitHub Actions**. If a task seems to need infrastructure,
that is a signal the design is being misread — re-read, don't build a server.

---

## 2. Architecture

```
Google Sheet  ──read──►  sync ──► _State (source of truth for delivery)
   (brand tabs)                      │
Google Drive  ──download──►  MediaPolicy (Pillow / ffmpeg)
                                     │
                              R2 (public, 60-day lifecycle)
                                     │
                              Publisher adapters ──► FB / IG / LI
                                     │
Google Sheet  ◄──batch write──  roll-up Status + _Log + Telegram summary
```

### Module layout
```
postpilot/
  cli.py             typer app; one function per command in §5
  config.py          config.yaml + .env loading -> pydantic Settings
  models.py          Post, BrandCreds, PreparedMedia, PublishResult, RemotePost, enums
  apis.py            GRAPH_API_VERSION, LINKEDIN_VERSION — pinned in ONE place
  sheets/            gspread client, tab schemas, batched read/write, sheet init
  drive.py           files.get(alt=media), supportsAllDrives=true, md5Checksum
  media/
    policies.py      one MediaPolicy class per target; POLICY_VERSION lives here
    normalise.py     Pillow + ffmpeg/ffprobe subprocess work
    store.py         MediaStore interface + R2Store (boto3, S3-compatible)
  state.py           _State read/write, lease, hashing, state machine transitions
  publishers/
    base.py          Publisher Protocol
    facebook.py  instagram.py  linkedin.py
  reconcile.py       unknown -> published | needs_review
  summary.py         Telegram daily digest
  logging.py         structured logs, token redaction
spike/               Phase 0 throwaway scripts — NOT imported by postpilot/
tests/               pytest; fakes + respx fixtures only
docs/                DECISIONS.md, MEDIA_POLICIES.md, HOW_TO_ADD_A_POST.md
```

`spike/` is throwaway. Nothing in `postpilot/` may import from it.

---

## 3. The rules that matter most

### 3.1 The delivery guarantee — word it exactly like this in docs
> *PostPilot never knowingly publishes the same post to the same platform
> twice. Once success is recorded for a platform, it is never automatically
> published again. If a remote API result is ambiguous, PostPilot stops and
> asks a human instead of retrying.*

Every design choice below exists to keep that sentence true. When in doubt
between "might double-post" and "might need a human", **choose the human.**

### 3.2 Each (post, platform) is an independent job
`_State` has one row per `(Post ID, Platform)` and is the real source of truth.
The brand tab's `Status` is a *roll-up for humans*, never something the code
reads back as truth. IG failing must not affect FB.

States: `scheduled → publishing → published | retryable_failed |
permanent_failed | unknown`, plus `invalid ↔ scheduled` and `skipped`.

- **retryable_failed** — 429/5xx/timeout **before the request was sent**.
  Back off exponentially via `Next Attempt At`. Max 3 attempts, then permanent.
- **permanent_failed** — 4xx validation, invalid token, attempts exhausted.
  Never auto-retried.
- **unknown** — the request left the machine and we have no definitive answer:
  timeout/connection reset *after* sending, or any exception between the API's
  2xx and the `_State` write. **Never blindly retried.** Goes to reconciliation.

### 3.3 Lease before you call
Write `publishing` + a fresh `Attempt ID` + `Started At` to `_State`
**before any API call**. A `publishing` row younger than **20 minutes** belongs
to another run — skip it. Older than 20 minutes → the run crashed → `unknown`.
This is what makes two concurrent workflow runs safe.

### 3.4 Content hash drives re-validation
Hash = brand, schedule, platforms, type, captions, link, Drive file IDs + md5s.
- Hash changed **and** platform is `invalid` / `permanent_failed` /
  `retryable_failed` → reset to `scheduled`, attempts 0. A corrected row
  retries itself, with no terminal.
- Hash changed **and** platform is `published` → do **nothing** except a `Notes`
  warning ("published version differs from Sheet"). Never republish.

### 3.5 Look rows up by ID, never by position
Rows get reordered, inserted and deleted by humans mid-flight. `ID`
(`gi-0042`-style, tool-assigned on first sync, immutable) is the key
everywhere. Parsing is defensive: strip whitespace, accept `TRUE`/`true`/`yes`,
tolerate blank rows and reordered columns.

### 3.6 Validation is per platform
`type=text` + IG invalidates **IG only** — FB and LinkedIn still publish that
row. Never let one platform's validation failure block another's.

### 3.7 Sheets API budget
**One read per tab per run, one batch update per tab per run.** Never read or
write a cell at a time in a loop. `_Log` is append-only, batched.

### 3.8 Media is stateless
```
Drive metadata (id, md5Checksum) → deterministic R2 key → HEAD → exists? reuse : download → normalise → PUT
```
R2 key: `{brand}/{post_id}/{platform}/{policy_version}-{drive_md5}.{ext}`.
**No local cache file.** Bump `POLICY_VERSION` (`v1` → `v2`) whenever any
normalisation rule changes, so previously transformed files are never reused.

### 3.9 Secrets
Flat, one per value (see `.env.example`). Never in the Sheet, never in the
repo, never in a log line — all HTTP logging goes through the redactor. The
public repo holds **code and docs only**.

### 3.10 GitHub Actions is the only routine live publisher
Local live publishing requires **both** `--live` and `--confirm`.
`--dry-run` runs the whole pipeline — Sheet read, normalise, R2 upload — and
prints exactly what each API call *would* send, but **never writes the lease**.

---

## 4. Sheet contract

| Tab | Purpose |
|---|---|
| `_Brands` | `Brand Name \| Slug \| Enabled Platforms \| Facebook Page ID \| Instagram User ID \| LinkedIn Org URN \| Drive Folder ID \| Default Hashtags \| Active` |
| `<slug>` | one tab per brand, named exactly as its `Slug` |
| `_State` | hidden; one row per `(Post ID, Platform)`; the real truth |
| `_Log` | append-only `Timestamp \| Brand \| Post ID \| Platform \| Action \| Result \| Details` |

**Brand tab, user-filled:** `ID | Date | Time | Platforms | Type | Media |
Caption | Caption (Facebook) | Caption (Instagram) | Caption (LinkedIn) |
Link | Action`

**Brand tab, tool-owned (protected):** `Status | Published URLs | Error |
Attempts | Last Run | Notes`

Rules:
- `Platforms` — comma list of `FB, IG, LI`, must be a subset of the brand's
  enabled platforms.
- `Type` — `image` (exactly 1 image) · `carousel` (2–10 images) · `reel`
  (exactly 1 video) · `text` (0 media). Counts are **strict**.
- `Media` — file names inside the brand's Drive folder, or Drive share links,
  comma-separated; order = carousel order. A name matching **more than one**
  file is an error ("ambiguous") — **never guess**. Google-native files
  (Docs/Slides/Sheets) are rejected.
- `Link` — optional URL appended to the FB and LinkedIn captions. **IG ignores
  it** (no clickable links in IG captions).
- `Default Hashtags` — appended to the **Instagram** caption only, on a new
  line, and **only when the resolved IG caption contains no `#`**. Never
  appended to FB or LinkedIn.
- `Action` — human dropdown, blank by default: `retry` (→ `scheduled`,
  attempts 0) · `mark published` (→ `published`, Remote URL `manual`) ·
  `skip` (→ `skipped`). The tool acts on it, then **clears the cell**. This is
  the non-technical recovery path.
- `Status` roll-up: `draft` (no date) · `invalid` · `scheduled` · `publishing`
  · `published` · `partial` · `failed` · `needs_review`.

**Timezone:** Sheet `Date`/`Time` are **Asia/Karachi**. Everything internal is
**UTC**. Convert at the boundary, exactly once.

---

## 5. CLI surface

| command | purpose |
|---|---|
| `postpilot init` | write `config.yaml`, `.env.example`, folders |
| `postpilot sheet init [--brand slug]` | create/repair tabs, headers, dropdowns, protections, hide `_State` |
| `postpilot sync` | validate, assign IDs, hash, write `_State` + `scheduled`/`invalid` |
| `postpilot prepare [--post id]` | download → normalise → upload media |
| `postpilot publish [--dry-run] [--live --confirm] [--brand s] [--post id]` | the run algorithm |
| `postpilot status [--brand slug]` | rich table: upcoming / published / failed / needs-review |
| `postpilot summary` | Telegram daily digest |
| `postpilot doctor` | full environment check |
| `postpilot auth meta --brand slug` / `auth linkedin` | guided token acquisition |

Note `auth linkedin` takes **no** `--brand` (one token, all pages) — this
differs from the original brief per §0.1.

---

## 6. Phases — stop after each and demo

| # | Phase | State |
|---|---|---|
| 0 | **API access spike** — FB + IG publish path, R2 public read (r2.dev), service-account Sheet/Drive access; record real media limits in `docs/MEDIA_POLICIES.md`. LinkedIn deferred. | **COMPLETE — live post published to both platforms, see §10** |
| 1 | Models + Sheet: `init`, `sheet init`, `sync`, `status`, `_State`/`_Log`, tests | **COMPLETE — runs against the real Sheet; 79 tests green** |
| 2 | Drive + media + R2: policies, normalisation, deterministic keys, `prepare`, `doctor` | **COMPLETE — 146 tests green; doctor 21 ok / 3 warn / 0 fail** |
| 3 | State machine + dry-run: lease, hashes, `Action`, reconciliation hooks, **all failure-injection tests green with fake publishers** | **COMPLETE — 177 tests green, all 10 scenarios covered** |
| 4 | Facebook adapter + reconciliation + one real image post | **COMPLETE — adapter + reconciliation live-verified; live post deferred to the operator** |
| 5 | Instagram adapter (image, carousel, reel) + publishing-limit check + container reconciliation | **COMPLETE — reconciliation live-verified read-only** |
| 6 | LinkedIn adapter + `auth linkedin` + token refresh | **written, UNVERIFIED — still blocked on API access** |
| 7 | Automation: workflows, summary (Telegram **or** `_Log` fallback, §0.5), launchd script, docs polish | **COMPLETE** |

**Stop at the end of each phase and show the operator what works before
continuing.** Keep this table's "State" column current — it is how the next
session knows where things stand.

---

## 7. Testing rules

**No test touches a real API or Google.** Fakes and `respx` fixtures only.
Phase 3 cannot be called done until every one of these passes:

| scenario | required outcome |
|---|---|
| crash before the API request | retried next run |
| crash after 2xx, before `_State` write | `unknown` → reconciled or `needs_review`, **never re-sent** |
| Sheet write fails after FB success | `unknown`; IG/LI unaffected |
| IG succeeds, LI returns 429 | `partial`; LI retried with backoff |
| runner dies during ffmpeg | lease expires → `unknown` → reconciliation finds nothing → `scheduled` again |
| token expired mid-run | `permanent_failed`, clear error; summary warns |
| same workflow starts twice | second run skips leased rows |
| user edits caption while publishing | published hash recorded; `Notes` warning |
| user fixes invalid media name | `invalid` → `scheduled` automatically |
| user sets `Action=retry` on failed row | `scheduled`, attempts reset, `Action` cleared |

Plus unit tests for row parsing, timezone/due selection, media policies
(ffprobe fixtures) and each adapter against recorded HTTP fixtures.

---

## 8. Engineering standards

- Pydantic models for **everything crossing a boundary** (Sheet row, API
  request/response, config).
- Defensive Sheet parsing; look up by `ID`, never position.
- Structured logging; **tokens redacted** in every log path.
- Pin API versions in `postpilot/apis.py` only; record tested versions in
  `docs/DECISIONS.md`.
- `docs/DECISIONS.md` — one paragraph per design decision, explaining **why**.
  Add to it as decisions are made, not at the end.
- Adapters: backoff on 429/5xx (max 3 attempts), **no retry on 4xx**.
- Stack: Python 3.12, `uv`, `typer`, `rich`, `pydantic`, `httpx`, `boto3`,
  `gspread` + Drive API v3, `Pillow`, `ffmpeg`/`ffprobe` via subprocess,
  `pytest`, `respx`, `ruff`.

---

## 9. Local environment (verified 2026-09-23)

`python3` 3.12.6 · `git` 2.50.1 · `uv` 0.12.17 · `ffmpeg`/`ffprobe` 9.0.2
(installed via Homebrew) · `gh` **not installed** (no remote yet, §0.3).
`ffmpeg`/`ffprobe` are preinstalled on `ubuntu-24.04`; `doctor` checks for them.

---

## 10. Phase 0 findings (complete, 2026-09-23)

**Phase 0 passes with no failures.** Every access the tool depends on for
Facebook, Instagram, Google and R2 is proven against the real accounts.

| Area | Result |
|---|---|
| Google service account | PASS - `postpilot@postpilot-509419.iam.gserviceaccount.com` |
| Sheet read / write | PASS - "PostPilot Content Calendar" (`1jO8T0SZ_vyIzhMok2c-nMSG81TMQCxUyOmn5mTwqbi4`); tabs `_Brands`, `_State`, `_Log` exist but are **empty** |
| Drive folders | PASS - both readable; **both empty**, so `md5Checksum` is still unproven |
| R2 bucket / PUT / HEAD / DELETE | PASS - `postpilot-media` |
| **R2 anonymous public GET** | **PASS** - the check that matters most; Meta and LinkedIn will be able to fetch our media |
| R2 lifecycle rule | WARN - not readable with an object-scoped token; **still needs manual confirmation** in the Cloudflare dashboard |
| Meta tokens (all 3 Pages) | PASS - long-lived Page tokens, **no expiry** |
| Instagram | PASS - `instagram_basic` + `instagram_content_publish` granted; quota 0/100 |
| Telegram | SKIP by choice (§0.5) |
| LinkedIn | not checked - access pending (§0.2) |

### The brand roster — confirmed, and it seeds `_Brands`

**Two brands are in scope.** A One Care and TOPSOL are Pages the token happens
to administer but are **not** being onboarded; their tokens were removed from
`.env` again. If either is ever added, one command regenerates it:
`uv run python spike/meta_exchange_token.py --brand grandinvitation --write-env`.

| Brand Name | Slug | Enabled Platforms | Facebook Page ID | Instagram User ID | LinkedIn Org URN | Drive Folder ID |
|---|---|---|---|---|---|---|
| Grand Invitation | `grandinvitation` | `FB, IG` | `1355072654348977` | `17841432916654917` | — | `13M2wXeZlKX71PCsm3qLkPbIe5VbmALBi` |
| Restockly POS | `restocklypos` | `LI` | — | — | *pending* | `1LEf9Dqlm7GefhpzPRMI8HFR4hXbrqV-Z` |

Two consequences worth stating plainly, because they shape Phases 1-6:

- **`restocklypos` is LinkedIn-only, and LinkedIn access is pending.** So it
  has no publishable platform until Phase 6. Phase 1 resolved how to represent
  that, and the answer is a **three-way** distinction, not the two-way one
  originally sketched here:
  - a *problem* is structurally wrong and must be fixed (duplicate slug,
    colliding ID prefix) — these fail the command;
  - a *warning* is a known-incomplete configuration the tool handles correctly
    (LinkedIn enabled before its URN exists) — reported, never fatal;
  - a row whose platform has no target ID is marked `invalid` with an error
    that says explicitly *"brand setting … (nothing wrong with this row)"*, so
    the teammate is not sent to fix a row that is already correct.

  Marking such rows `scheduled` was the original plan and is wrong: a row that
  can never publish must not claim it is waiting to. Its org URN goes into
  `_Brands` when access lands, and every affected row re-opens automatically.
- **`grandinvitation` is the only brand exercisable end to end right now**, so
  it is the test brand for Phases 1-5. Every real publish test runs against it.

This pairing is actually a good draw: one brand with two platforms and one with
a platform we cannot reach yet is precisely the "not every brand has every
platform" shape the brief calls for, and it exercises per-platform validation
from Phase 1 rather than leaving it untested until late.

### What Phase 0 resolved along the way

- **Meta tokens were wrong twice**, in two different ways: first a short-lived
  Page token, then a short-lived *user* token in a `META_PAGE_TOKEN_*` slot.
  `spike/meta_exchange_token.py` now performs the whole exchange
  (`fb_exchange_token` -> `/me/accounts`) and writes the resulting
  never-expiring Page tokens into `.env`. **It is the working prototype of
  `postpilot auth meta`** — Phase 4 should build on it, not restart it.
- **The R2 token was read-only.** Replaced with Object Read & Write; the
  anonymous public read now passes.
- **Amendment 0.2(5) does not apply**: `instagram_basic` is granted and
  @grand.invitation resolves. Nothing to work around.

### The end-to-end publish test — PASSED 2026-09-23

`spike/publish_end_to_end.py` ran the entire chain against the real accounts
and published to both platforms:

```
Drive (PNG, md5 47bd99c0…)
  -> download
  -> normalise per platform (PNG -> JPEG, 1254x1254, 311 KB)
  -> R2 deterministic key, HEAD-then-PUT
  -> public URL over r2.dev
  -> Facebook /photos  +  Instagram container -> FINISHED -> media_publish
```

| Result | |
|---|---|
| Facebook | `post_id 1355072654348977_122114927469466419` |
| Instagram | `media_id 18026718245885330`, https://www.instagram.com/p/DdmxphpnDhS/ |

**What this actually de-risked**, beyond "it works":

- **The deterministic key + HEAD reuse path is real.** The live run reused the
  objects the dry run had uploaded (`R2 reuse - object already present`),
  which is the behaviour Phase 2 depends on to stay inside the R2 free tier.
  Same Drive md5, same policy version, same key.
- **PNG -> JPEG conversion is mandatory, not cosmetic.** The source was a PNG;
  Instagram accepts JPEG only. Converting for *both* platforms (rather than
  only for IG) also sidesteps Facebook's "PNG over 1 MB may appear pixelated"
  caveat. Confirms the §3.1 normalisation decision.
- **`post_id` really is distinct from `id`.** Facebook returned photo id
  `122114927445466419` and post_id `…_122114927469466419` - different numbers.
  Recording the wrong one gives a `Remote URL` that 404s and breaks
  reconciliation matching.
- **Per-platform captions work end to end.** Facebook got the clean caption,
  Instagram got the same text plus hashtags - the path the Sheet's
  `Caption (Facebook)` / `Caption (Instagram)` columns will use.
- **The IG container flow needs no retry logic at this size.** Container went
  to `FINISHED` on the first poll. The 5-minute timeout -> `unknown` path
  remains untested and should be exercised with a video in Phase 5.

**Phase 0 is complete.** Every Facebook, Instagram, Google and R2 dependency is
proven end to end. LinkedIn remains the only untested platform, by design.

### Cleanup owed

The test posts are live on both platforms and **the tool cannot delete them** -
v1 has no deletion. They must be removed by hand from the Page and the IG
account. The R2 objects under `grandinvitation/spike-0001/` expire themselves
via the 60-day lifecycle rule.


---

## 11. Phase 1 findings (complete, 2026-09-23)

Runs against the real Sheet (`PostPilot Content Calendar`). 79 tests green,
none touching a real API or Google.

**Built:** `postpilot/{apis,config,models,logging,sync,status,cli}.py` and
`postpilot/sheets/{schema,client,parse,state,setup}.py`. `init`, `sheet init`,
`sync` and `status` work; `prepare`, `publish`, `summary`, `doctor` and `auth`
exit with an explicit "arrives in Phase N" message rather than pretending.

**Verified end to end on the real Sheet:**

| | |
|---|---|
| `sheet init` | created `grandinvitation` and `restocklypos` tabs, applied 20 structural changes (freeze, bold, dropdowns, warn-only protection, hid `_State`) |
| `sync` | assigned `gi-0001`…`gi-0003`, wrote 6 ranges, rewrote `_State` (6 rows), appended `_Log` |
| idempotency | second `sync` assigned 0 new IDs and changed nothing |
| self-healing | fixing a 1-file carousel re-opened **both** its platforms `invalid -> scheduled` with no human intervention |
| per-platform validation | a `text` row on `FB, IG` shows `scheduled` with `IG: Instagram cannot post without media` — Facebook is unaffected |

**Decisions made during the phase** (full reasoning in `docs/DECISIONS.md`):

- **Post IDs** are `{prefix}-{NNNN}` with the prefix derived from the brand
  name's initials (`Grand Invitation` -> `gi`). Prefix collisions across brands
  are detected at load and reported, because colliding IDs would silently
  merge two brands' state.
- **Protection is `warningOnly`.** Hard protection is enforced by editor list,
  and getting that wrong with a service account can lock the Sheet's owner out
  of their own columns. The risk being managed is an accidental paste, which a
  warning already prevents.
- **Ambiguous dates warn instead of guessing silently.** `03/04/2026` is read
  day-first (Pakistan convention) *and* leaves a note in `Notes` saying so.
  `YYYY-MM-DD` is what the guide tells people to use.
- **A published row's content hash is never updated.** That is what keeps the
  "published version differs from the Sheet" warning true on every later run
  rather than disappearing after one.

**Carried into Phase 2 and now done:** `content_hash()` hashes resolved Drive
file IDs + md5s, and `HASH_VERSION` was bumped `h1` -> `h2`.


---

## 12. Phase 2 findings (complete, 2026-09-23)

**Built:** `postpilot/drive.py`, `postpilot/media/{policies,normalise,store}.py`,
`postpilot/prepare.py`, `postpilot/doctor.py`. `prepare` and `doctor` are real
commands now; `publish`, `summary` and `auth` remain phase-stubbed.

**Verified against the real services:**

| | |
|---|---|
| `doctor` | 21 ok · 3 warnings · **0 failures** (warnings are LinkedIn pending, Telegram unconfigured, and restocklypos' missing org URN — all expected) |
| `prepare --dry-run` | reported both platform keys, uploaded nothing |
| `prepare` | uploaded 2 objects (FB + IG) from one Drive PNG |
| `prepare` (again) | **0 uploaded, 2 reused** — the HEAD-before-PUT path |
| `sync` with Drive | caught `no file named 'second-image.png'` and marked the row `invalid` |

**The hash now depends on Drive, not on typing.** `HASH_VERSION` is `h2`:
`content_hash()` hashes resolved file IDs + md5s, so **renaming a file in Drive
no longer re-opens a post, while replacing its bytes does**. That is the
correct behaviour and was not achievable in Phase 1.

### Design notes worth keeping

- **Pad, don't crop** (beyond a 5% tolerance). Cropping silently removes part
  of a design someone made deliberately — a wedding invitation with its date
  cropped off is worse than one with soft blurred bars.
- **Refuse, don't mangle.** Duration limits raise rather than trim: a
  4-minute video is not a 90-second video with the end cut off. The message
  lands in the teammate's `Error` column.
- **Carousels take the *median* aspect**, clamped into range, so one odd image
  cannot drag a whole set into heavy padding.
- **Carousel keys are indexed.** Two identical files in one carousel share an
  md5 and would otherwise collapse onto a single key, silently losing an item.
- **A video canvas is sized from the source's LONG edge.** Using the edge that
  matches the target orientation turns a 1920x1080 clip into a 608x1080
  portrait canvas — technically 9:16, but postage-stamp sized. Caught in
  testing, and now asserted.

### Two bugs found by running it, not by reading it

1. **ffmpeg input ordering.** The silent-audio input was declared after the
   output options, so ffmpeg parsed it as an option *on the output* and failed
   with a misleading message. Inputs must come first.
2. **rich markup ate bracketed text again** — `Drive [grandinvitation]`
   rendered as `Drive `. Same class of bug as in the spike. All
   data-derived strings reaching the console now go through
   `rich.markup.escape`, in `cli.py` and `status.py`.

**Carried into Phase 3 and now done:** `publish` calls `prepare_post_platform`
itself, after the lease is written.


---

## 13. Phase 3 findings (complete, 2026-09-23)

**Built:** `postpilot/publishers/{base,registry}.py`, `postpilot/reconcile.py`,
`postpilot/publish.py`. `publish` is a real command with the two-flag gate.
**All ten failure-injection scenarios pass**, plus two extra the work surfaced.
177 tests, none touching a real API or Google.

### The run order IS the safety property

```
1. sync                     validate, assign IDs, reconcile _State with the Sheet
2. Action column            act, then clear the cell
3. expire stale leases      publishing older than 20 min -> unknown (never a retry)
4. reconcile unknowns       including the ones step 3 just created
5. select what is due
6. lease THIS row, flush    written to _State BEFORE its own API call
7. publish, record          result written the moment it is known
8. roll up, _Log
```

Two orderings changed during the phase, both because a test failed:

- **Leases expire before reconciliation, not during selection.** Originally a
  stale lease became `unknown` at step 5, *after* reconciliation had already
  run — so a crashed run took two further runs to resolve instead of one.
- **Each row is leased immediately before its own API call**, not all of them
  in one batch up front. With batch leasing, a run that died partway left rows
  it never reached sitting `publishing`, which then needed a lease expiry and a
  reconciliation to discover that nothing had happened. Now an unattempted row
  is simply still `scheduled`.

### Reconciliation: the one place we reason about uncertainty

The rule that makes it safe:

- **A successful lookup that does not contain our post is evidence of
  absence** — so it is safe to schedule again. Nothing can be duplicated
  because nothing is there.
- **A lookup that fails is evidence of nothing.** The state stays `unknown`,
  rolls up to `needs_review`, and a human decides.

This is the only reading under which the guarantee survives. "If a remote API
result is ambiguous, PostPilot stops and asks a human" — a *negative* result
from a working API is not ambiguous. Matching requires caption **and** time
(±30 min); either alone is too loose.

### What happens when the Sheet itself fails mid-run

Nothing can be written, because the Sheet is the broken thing. So the recovery
is the lease: it was written **before** the API call, so `_State` still says
`publishing`. That row ages out into `unknown` and the next run settles it by
looking at the platform. The run is allowed to die here rather than papering
over it — and `test_sheet_write_failure_after_success_is_covered_by_the_lease`
plus `test_the_surviving_lease_then_resolves_to_published_next_run` assert the
whole journey.

### Other decisions

- **An adapter that raises becomes `unknown`, not a retry.** If an adapter
  throws instead of classifying, we cannot know whether the request was sent.
- **Media problems are permanent, not retryable.** A missing file is not fixed
  by waiting; it is fixed by a human.
- **A missing adapter is a permanent failure on that platform only.** The other
  platforms on the row still publish — which is exactly what `restocklypos`
  will need until Phase 6.

### Verified against the real Sheet

`publish --live` without `--confirm` is refused. `publish --dry-run` ran the
whole pipeline and reported `gi-0001 FB: would publish` / `IG: would publish`,
and `_State` afterwards still read `attempts=0, attempt_id=''` — **the dry run
wrote no lease**, which is the property that makes it safe to run any time.

**Phase 4 note:** `postpilot/publishers/registry.py` returns an empty dict.
Adding Facebook is one entry there plus one adapter module; nothing else in the
run algorithm changes.


---

## 14. Phase 4 findings (complete, 2026-09-23)

**Built:** `postpilot/publishers/{http,facebook}.py`, registry wired.
201 tests. Image, carousel, reel and text all covered with `respx` fixtures,
plus every classification branch.

### `/published_posts`, not `/feed` — found by calling the real API

`/{page}/feed` fails with `(#10) This endpoint requires ... Page Public Content
Access` on an ordinary Page token, because it *also* returns visitor posts.
`/{page}/published_posts` works with the token we already have, and is
semantically what reconciliation wants: things **this Page published**. Visitor
posts in the result set would have been a false-match risk.

### Reconciliation verified against real data

`find_recent` read 12 real published posts, and `find_match` correctly matched
the Phase 0 live post by caption + timestamp, returning exactly the `post_id`
recorded back then (`1355072654348977_122114927469466419`). That is the whole
`unknown -> published` path proven against the real Graph API, without
publishing anything.

### A half-built carousel is `unknown`, not a retry

Carousel children are created with `published=false`, and they exist on the
Page's object graph the moment they are created. So a failure *partway through*
the children, or between the children and the feed post, cannot be retried —
retrying would create a second set and could double-post. Those two branches
return `unknown` with a message saying how many photos are orphaned and that
they need clearing by hand. A failure on the **first** child is still a clean
permanent failure, because nothing exists yet.

### The live post is deliberately deferred

The operator was asleep. Each live post is public and the tool cannot delete
it, and Phase 0 already proved the real publish path end to end with these
credentials. To do it:

```bash
# set the Date on a row to today, then:
uv run postpilot publish --live --confirm --post <id> --brand grandinvitation
```


---

## 15. Phase 5 findings (complete, 2026-09-23)

**Built:** `postpilot/publishers/instagram.py`. 224 tests.

### Instagram is the riskiest adapter, and the code says why

Everything is a two-step container flow, and **`media_publish` has no
idempotency key**. So the window between that request leaving and its answer
arriving is the one place a blind retry publishes twice. Every non-4xx failure
from that point returns `unknown` **carrying the container ID**, so
reconciliation can ask the API what became of it. A 4xx there stays permanent —
"already published" is a definitive answer, not an ambiguous one.

### Where Instagram and Facebook deliberately differ

| situation | Facebook | Instagram |
|---|---|---|
| carousel child fails partway | `unknown` — children are real objects on the Page and a retry duplicates them | **permanent** — containers are invisible until publish and expire on their own, so a retry is clean |
| quota | n/a | checked **before** attempting |

That asymmetry is not an inconsistency; it follows from what each platform
leaves behind on a partial failure.

### The quota gate

`/{ig_user}/content_publishing_limit` is checked before every attempt. If it is
exhausted the platform is left `retryable_failed` with a plain message, so the
post waits rather than burning one of its three attempts on a rejection that
was certain. If the quota itself cannot be read, publishing proceeds — a check
that cannot be made must not block delivery.

### Verified live, read-only

`remaining_quota` -> 99 of 100. `find_recent` read 9 real media, and
`find_match` resolved the Phase 0 Instagram post by caption + timestamp,
returning `18026718245885330` — exactly the media ID Phase 0 recorded, with
permalink `DdmxphpnDhS`. Both platforms' `unknown -> published` paths are now
proven against real data without publishing anything.


---

## 16. Phase 6 (LinkedIn) — written, unverified, gated

**Community Management API access was still pending.** The adapter
(`publishers/linkedin.py`) and the auth flow (`auth/linkedin.py`) are written
from the published documentation and covered by `respx` fixtures, but **no line
of this has touched the real API**. Three things keep that honest:

1. **The registry only adds LinkedIn when `LINKEDIN_ACCESS_TOKEN` is set.** It
   cannot be reached by accident; a token appearing is a deliberate act.
2. **`doctor` warns** that the adapter is unverified, every run.
3. **The fixtures are documentation-derived, and say so.** When the first real
   run disagrees with one, fix the code *and* the fixture together — that is
   how they become real fixtures.

### What is implemented

| | |
|---|---|
| text | `POST /rest/posts`, no `content` block |
| image | `images?action=initializeUpload` -> PUT bytes -> URN in `content.media` |
| carousel | several images -> **MultiImage** post (LinkedIn has no carousel type) |
| video | `videos?action=initializeUpload` -> PUT each 4 MB part, **collect every ETag** -> `finalizeUpload` |
| reconciliation | `GET /rest/posts?q=author` (needs `r_organization_social`) |
| auth | authorization-code flow on `localhost:8765`, plus `--refresh` |

### Things worth knowing before the first real run

- **LinkedIn wants the bytes, Meta wants a URL.** Meta fetches media from our
  R2 URL itself; LinkedIn makes us upload. So the adapter downloads our own R2
  object and PUTs it — which is why `fetch` is injectable and why a failure
  there is `retryable` rather than `permanent`.
- **The post URN comes back in the `x-restli-id` header**, not the body.
- **`finalizeUpload` needs every part's ETag in order.** A part that uploads
  but returns no ETag makes the video unfinishable, so that is `unknown`.
- **A refresh that returns no new refresh token keeps the old one.** LinkedIn
  does not always issue one, and blanking it would lock the operator out.
- Access tokens last ~60 days, refresh tokens ~1 year. `doctor` warns from 7
  days out. **This is the most likely thing to silently break** — a scheduler
  needing a human every two months stops working while nobody is watching.

`restocklypos` is LinkedIn-only, so it publishes nothing until this is verified
and its org URN is filled into `_Brands`.


---

## 17. Phase 7 findings (complete, 2026-09-23)

**Built:** `postpilot/summary.py`, three workflows, the launchd installer,
docs polish. 264 tests.

### The digest is never silently skipped

It is the only routine signal that the scheduler is alive, so "Telegram is not
set up" must not mean "no digest". When Telegram is unconfigured *or refuses*,
the same text goes to the `_Log` tab — durable, timestamped, and in the place
the teammate already looks. `send()` never raises: a notifier that crashes the
run would turn a reporting problem into a delivery problem.

### Workflow decisions

- **`concurrency: cancel-in-progress: false`.** Cancelling a run between an
  API's 2xx and our `_State` write is *exactly* how a post gets published
  twice. A queued second run is fine; a cancelled first one is not.
- **The cron is staggered off the hour** (`7,22,37,52`). GitHub's scheduler is
  busiest at `:00` and delays runs there, and the 15-30 minute SLA has no room
  for that.
- **`keepalive.yml` exists because GitHub disables scheduled workflows after
  60 days of repository inactivity, silently.** A scheduler that quietly stops
  scheduling is this project's worst failure.
- **The failure artifact is safe to keep** because redaction happens in
  `postpilot.logging` before anything is printed, not at upload time.

### Adding a brand touches three places

A new brand needs a `_Brands` row, a `META_PAGE_TOKEN_<SLUG>` secret, **and**
the same line added to the `env:` block of both workflows. Actions cannot
enumerate secrets, so each one must be named. This is the one piece of manual
wiring the design does not remove, and it is called out in the README.

### launchd is an alternative, not an addition

`scripts/install-local-launchd.sh` uses the same stagger, and prints a loud
instruction to disable the Actions cron. Two schedulers is the situation the
lease *survives*, which is not the same as wanting it: each run duplicates
work, and every ambiguous result costs a human a look at the platform.


---

## 18. Adversarial testing pass (2026-09-23)

A deliberate bug hunt after Phase 7, probing the paths least covered by the
phase work. **Seven real defects, two of them able to publish twice.** All
fixed, all with named regression tests in `tests/test_regressions.py`.
302 tests.

| # | Defect | Severity |
|---|---|---|
| 1 | **`--brand X` reconciled EVERY brand's `unknown` rows.** Brand Y's captions were never loaded, so every match failed, so a post that HAD published was returned to `scheduled` and republished on the next full run. | **double publish** |
| 2 | **Caption matching compared only the first 60 characters.** A series ("…Part ONE" / "…Part TWO") collided, and a short caption like "Hi" matched anything starting with it — marking the WRONG post published, losing ours and recording a stranger's URL. | **wrong/lost post** |
| 3 | **EXIF orientation was ignored.** Phone cameras tag rotation instead of rotating pixels, so portrait photos published sideways, and the aspect logic padded the wrong axis. | visible on every phone photo |
| 4 | **Duplicate post IDs were accepted.** Two rows sharing an ID shared `_State`, so publishing one marked the other published and it never went out. Copying a row is the most natural thing a teammate does. | silent lost post |
| 5 | **`published` + `invalid` rolled up to `scheduled`** — reads as "queued, nothing wrong" when half the row is live and the other half can never go. | misleading status |
| 6 | **Every `_State` write re-read the whole tab** just to learn its old extent. `publish` rewrites it several times a run. | wasted quota |
| 7 | **`--post <typo>` reported "nothing to do" and exited 0**, indistinguishable from success. | silent no-op |

### The two that mattered

**#1 and #2 are the same shape**: something that looked like a safe "not
found" was actually "we did not look properly". The reconciler's whole licence
to reschedule rests on a negative lookup being *evidence of absence* — and both
bugs produced negatives that were evidence of nothing. The fixes make the scope
explicit: `captions` is now the authoritative list of what this run loaded, a
state outside it is skipped untouched, and a post with no caption is handed to
a human rather than guessed at.

### Also hardened

- **`sync --skip-media` now requires `--dry-run`.** Without Drive the hash
  falls back to the Media text, so every hash differs and every failed row
  re-opens with its attempts reset. Verified: the dry run reports "2 re-opened".
- **`R2Store.exists` still raises on `AccessDenied`** rather than reporting
  "missing" — confirmed, since silently re-uploading forever would hide a
  broken token.

### Confirmed sound under abuse

Reordered columns · a deleted column · rows shorter than the header ·
non-breaking spaces, smart quotes and emoji · corrupt `_State` rows ·
header case/whitespace drift · 1×1 and 1×5000 images · CMYK, greyscale, LA,
palette-with-transparency, animated GIF · zero-byte, truncated and
HTML-pretending-to-be-JPEG files · 12000×12000 decompression-bomb shapes ·
audio-only "video" · 2-second clips · 3000×100 aspect ratios · every one of
the eight `PlatformState` values as a starting point for a due row · running
the same publish twice · hash stability across repeated syncs.
