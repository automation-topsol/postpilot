# PostPilot — working agreement for Claude Code sessions

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
5. **Telegram is optional.** It is not set up, and the tool must work without
   it. Missing `TELEGRAM_*` vars are a **`doctor` WARNING, never a failure**,
   and `postpilot summary` writes the daily digest to the **`_Log` tab**
   instead, so a summary is never silently lost. A missing notifier degrades
   reporting, not delivery — nothing about publishing may depend on it.
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
| 0 | **API access spike** — FB + IG publish path, R2 public read (r2.dev), service-account Sheet/Drive access; record real media limits in `docs/MEDIA_POLICIES.md`. LinkedIn deferred. | **spike built and run; blocked on 2 credential fixes — see §10** |
| 1 | Models + Sheet: `init`, `sheet init`, `sync`, `status`, `_State`/`_Log`, tests | not started |
| 2 | Drive + media + R2: policies, normalisation, deterministic keys, `prepare`, `doctor` | not started |
| 3 | State machine + dry-run: lease, hashes, `Action`, reconciliation hooks, **all failure-injection tests green with fake publishers** | not started |
| 4 | Facebook adapter + reconciliation + one real image post | not started |
| 5 | Instagram adapter (image, carousel, reel) + publishing-limit check + container reconciliation | not started |
| 6 | LinkedIn adapter + `auth linkedin` + token refresh — **blocked on API access** | blocked |
| 7 | Automation: workflows, summary (Telegram **or** `_Log` fallback, §0.5), launchd script, docs polish | not started |

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
`ffmpeg`/`ffprobe` are preinstalled on `ubuntu-latest`; `doctor` checks for them.

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
  has no publishable platform until Phase 6. That is legitimate, not an error
  state: its rows should validate, sit at `scheduled`, and simply never be
  selected for publishing — *not* be marked `invalid`. Phase 1 must therefore
  distinguish "this brand has no enabled platform I can publish to yet" from
  "this row is malformed", and Phase 3's dry-run should show its rows waiting
  rather than failing. Its org URN goes into `_Brands` when access lands.
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

### Still outstanding

1. **Drive folders are empty** — drop an image into
   `13M2wXeZlKX71PCsm3qLkPbIe5VbmALBi` so `md5Checksum` is confirmed and
   Phase 2 has something real to normalise.
2. **No live test post has been made.** Read-only checks prove access, not the
   publish path. One real image post is the honest end of Phase 0 and needs an
   explicit go-ahead: v1 cannot delete a published post.

Resolved: the 60-day R2 lifecycle rule was **confirmed by hand on 2026-09-23**
and recorded in `R2_LIFECYCLE_CONFIRMED`, so the check now reports it as
confirmed instead of warning on every run.
