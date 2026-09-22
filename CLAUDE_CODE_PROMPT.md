# Build "PostPilot" — a Google-Sheet-driven social media scheduler

You are a senior software engineer with strong system-design judgment. Build a small, reliable tool that publishes marketing posts (images, carousels, reels) to **Facebook Pages, Instagram Business accounts and LinkedIn Company Pages** for **several brands**, on a schedule defined in a **Google Sheet**, with media stored in **Google Drive**. It must be simple enough that a non-technical teammate can schedule posts with nothing but the Sheet and a Drive folder, and simple enough for me (a Python/FastAPI developer) to run and maintain without a server.

Work in this directory (it is empty). Initialise a git repo. Follow the phases in §13 in order and **stop after each phase** to show me what works before continuing. Do not add a web app, database server, Docker, Celery, Redis or any other infrastructure — this must stay a CLI + Sheet + GitHub Actions.

---

## 1. Requirements (decided — do not re-ask)

| Topic | Decision |
|---|---|
| Brands | 4–6 brands, each with its own pages/tokens. **Not every brand has every platform** — configured per brand. |
| Content source | **Google Sheet** (created by me, shared with a service account): one tab per brand + `_Brands`, `_State`, `_Log` tabs. Media in a **Google Drive folder per brand**. |
| Who edits | Me and a non-technical person. No terminal for them, ever. |
| Approval | None. Any row with a date/time in the past and no status is published. |
| Content types | Single image, carousel (2–10 images), reel/short video. Text-only allowed for FB/LinkedIn. |
| Captions | One `Caption` column, optional `Caption (Facebook)` / `Caption (Instagram)` / `Caption (LinkedIn)` overrides. |
| Media fixes | **Auto-fix** with Pillow + ffmpeg per platform policy (§5). Changes reported in the row's `Notes`. |
| Media hosting | **Cloudflare R2** public bucket behind a **custom domain** (e.g. `media.<mydomain>/postpilot/...`; `r2.dev` only for local testing). Objects expire via a **lifecycle rule (60 days)**. Storage behind a tiny `MediaStore` interface so it can be swapped. |
| Scheduler | **GitHub Actions** cron, staggered `7,22,37,52 * * * *`. **GitHub Actions is the only routine live publisher**; local live publishing requires `--live --confirm`. |
| Repo | **Public GitHub repo** (unlimited free Actions minutes; the repo holds only code/docs — never content or secrets). Add a monthly keep-alive commit step so GitHub doesn't disable the cron after 60 days of inactivity. If I later make it private, document the ~1 billed minute per run (≈2,900 min/month at 15-min cadence vs 2,000 free) and offer a 30-min cadence. |
| Timing SLA | Posts normally go out **within 15–30 minutes** of the requested time. State this in the non-technical guide. |
| Alerts | **Daily Telegram summary** at 09:00 Asia/Karachi: published / failed / needs-review / upcoming-24h per brand, plus token-expiry warnings. No per-post pings. |
| Volume | 20–50 posts/week total. Design for minimal cost: R2 within free tier at this volume. |
| Stack | Python 3.12, `uv`, `typer`, `rich`, `pydantic`, `httpx`, `boto3`, `gspread` + Google Drive API v3, `Pillow`, `ffmpeg`/`ffprobe` via subprocess, `pytest`, `respx`, `ruff`. |
| Timezone | Asia/Karachi for Sheet dates; UTC internally. |

## 2. Google Sheet layout

I create the Sheet and each brand's Drive folder by hand and share them with the service-account email (service accounts can't own Drive files). The tool must be able to **create/repair tabs inside that Sheet** (`postpilot sheet init`) with headers, dropdown validation, frozen header row, and protected tool-owned columns.

### `_Brands` tab (one row per brand)
`Brand Name | Slug | Enabled Platforms | Facebook Page ID | Instagram User ID | LinkedIn Org URN | Drive Folder ID | Default Hashtags | Active`

- Tab name for each brand's posts = `Slug`.
- `Default Hashtags` are appended to the **Instagram** caption only (on a new line after the caption) when the row's IG caption contains no `#`. Never appended to FB/LinkedIn.
- Secrets are **never** in the Sheet.

### Brand tab — user-filled columns
`ID | Date | Time | Platforms | Type | Media | Caption | Caption (Facebook) | Caption (Instagram) | Caption (LinkedIn) | Link | Action`

- `ID` — auto-filled by the tool on first sync (e.g. `gi-0042`), never changes, is the key everywhere.
- `Platforms` — comma list of `FB, IG, LI`; must be a subset of the brand's enabled platforms.
- `Type` — dropdown `image | carousel | reel | text`. Media counts are strict: image = exactly 1 image, carousel = 2–10 images, reel = exactly 1 video, text = 0 media.
- `Media` — comma-separated file names inside the brand's Drive folder, or Drive share links. Order = carousel order. A file name that matches **more than one** file in the folder is an error ("ambiguous"), never a guess. Google-native files (Docs/Slides) are rejected.
- `Link` — optional URL appended to FB/LinkedIn caption. IG ignores it.
- `Action` — human dropdown, blank by default: `retry | mark published | skip`. The tool consumes it (acts, then clears it). This is how the teammate recovers a `failed` or `needs_review` row without a terminal.
- Validation is **per platform**: `text` + IG invalidates IG only; FB/LinkedIn still publish.

### Brand tab — tool-owned columns (protected)
`Status | Published URLs | Error | Attempts | Last Run | Notes`

`Status` is a roll-up of the per-platform states in `_State`: `draft` (no date) · `invalid` · `scheduled` · `publishing` · `published` · `partial` · `failed` · `needs_review`.
`Attempts` shows e.g. `FB 1 / IG 3 / LI 1`; `Published URLs` shows `FB: … ; IG: … ; LI: …`.

### `_State` tab (hidden; machine state — the real source of truth)
One row per `(Post ID, Platform)`:
`Post ID | Brand | Platform | State | Attempts | Attempt ID | Remote ID | Remote URL | Content Hash | Last Error | Started At | Last Attempt At | Next Attempt At | Completed At`

### `_Log` tab
Append-only: `Timestamp | Brand | Post ID | Platform | Action | Result | Details`.

Write `docs/HOW_TO_ADD_A_POST.md` for the non-technical person: the columns, media naming and size rules, what each Status means, how to use `Action`, and the 15–30 minute timing note.

## 3. Google access

Service account only (no OAuth browser flow for Google). Credentials JSON in secret `GOOGLE_SERVICE_ACCOUNT_JSON`. Read/write Sheets with `gspread` using **batched** reads and writes (one read of each tab per run, one batch update per tab) to stay well under Sheets API quotas. Drive downloads via `files.get(alt=media)`; pass `supportsAllDrives=true` so Shared Drives work.

## 4. Delivery model — each (post, platform) is an independent job

**Guarantee (word it exactly like this in the docs):** *PostPilot never knowingly publishes the same post to the same platform twice. Once success is recorded for a platform, it is never automatically published again. If a remote API result is ambiguous, PostPilot stops and asks a human instead of retrying.*

Per-platform state machine in `_State`:

```
scheduled → publishing → published
                       → retryable_failed  (429/5xx/timeout before send; back off, Next Attempt At)
                       → permanent_failed  (4xx validation, token invalid, 3 attempts exhausted)
                       → unknown           (request left the machine, no definitive answer)
invalid ↔ scheduled    (re-validated every sync)
unknown → published | scheduled | skipped  (via reconciliation or human Action)
```

Run algorithm (`postpilot publish`):
1. `sync`: read brand tabs, assign IDs, validate per platform, compute **Content Hash** (brand, schedule, platforms, type, captions, link, Drive file IDs + md5s). If a row's hash changed since its `_State` rows were written and the platform is `invalid`, `permanent_failed` or `retryable_failed`, reset it to `scheduled` (attempts 0) — a corrected row retries itself. A hash change on a `published` platform does **nothing** except a `Notes` warning ("published version differs from Sheet").
2. Process `Action` column (retry → reset to scheduled; mark published → `published` with `Remote URL` = "manual"; skip → `skipped`), then clear it.
3. Select `_State` rows with `State ∈ {scheduled, retryable_failed}`, `Next Attempt At ≤ now`, schedule ≤ now.
4. **Lease** per platform: write `publishing` + new `Attempt ID` + `Started At` to `_State` *before* any API call. A `publishing` row younger than 20 min belongs to another run — skip. Older → treat as crashed → `unknown` (never blindly retry).
5. Prepare media (§5), call the adapter. Classify the result: definitive success → `published` with Remote ID/URL written **immediately**; definitive 4xx → `permanent_failed`; 429/5xx/connection error **before** the request was sent → `retryable_failed` with exponential backoff; timeout/connection reset **after** sending, or any exception between the API's 2xx and the `_State` write → `unknown`.
6. **Reconciliation** for `unknown` (FB, LinkedIn): list the page's/org's last 10 posts, match by caption text + created time within ±30 min of the attempt → `published`, else `needs_review`. For IG: query the container status by ID if known, else `needs_review`. Reconciliation runs once, at the start of the next run.
7. Roll up per-platform states into the brand-tab `Status`, batch-write, append `_Log`.
8. Workflow `concurrency: { group: postpilot-publish, cancel-in-progress: false }`.

## 5. Media pipeline (stateless — no local cache)

```
Drive metadata (id, md5Checksum) → deterministic R2 key → HEAD → exists? reuse : download → normalise → PUT
```

R2 key: `{brand}/{post_id}/{platform}/{policy_version}-{drive_md5}.{ext}`. Bump `policy_version` (`v1`, `v2`, …) whenever a normalisation rule changes so old transformed files are never reused. No local cache file.

One `MediaPolicy` class per target; **verify every numeric limit against the current official docs in Phase 0 and record them in `docs/MEDIA_POLICIES.md`** — do not trust the figures below, they are starting points:

- `InstagramImagePolicy` — JPEG, aspect 4:5 … 1.91:1 (pad, blurred background, rather than crop unless within 5 %), ≤ 8 MB, long edge ≤ 1440 px.
- `InstagramCarouselPolicy` — as above, all items share one aspect ratio.
- `InstagramReelPolicy` — MP4 H.264/AAC, 9:16, duration and size per current docs, 30 fps, faststart.
- `FacebookImagePolicy` — JPEG/PNG ≤ 4 MB.
- `FacebookReelPolicy` — separate from IG (different duration limits); 9:16, H.264/AAC.
- `LinkedInImagePolicy` — JPEG/PNG ≤ 8 MB. `LinkedInVideoPolicy` — MP4, per docs.

ffmpeg/ffprobe are preinstalled on `ubuntu-latest`; `doctor` checks them.

## 6. Publishing adapters

```python
class Publisher(Protocol):
    platform: Platform
    def publish(self, post: Post, media: list[PreparedMedia], creds: BrandCreds) -> PublishResult: ...
    def find_recent(self, creds: BrandCreds, since: datetime) -> list[RemotePost]: ...   # for reconciliation
```

Pin API versions in one place (`postpilot/apis.py`: `GRAPH_API_VERSION`, `LINKEDIN_VERSION`) and note the tested versions in `docs/DECISIONS.md`.

- **Facebook Page** — Graph API. Image: `/{page}/photos`; carousel: photos with `published=false` then `/{page}/feed` with `attached_media`; reel: `/{page}/video_reels` start/upload/finish. Long-lived Page Access Token per brand.
- **Instagram Business** — container flow: `/{ig_user}/media` (image_url / video_url + `media_type=REELS` / carousel children), poll `status_code` until `FINISHED` (timeout 5 min → `unknown` with container ID stored in `Remote ID` for reconciliation), then `/media_publish`. Check `/{ig_user}/content_publishing_limit` first; if exhausted, leave `scheduled` and note it.
- **LinkedIn Company Page** — Posts API (`POST /rest/posts`, `LinkedIn-Version` header, scope `w_organization_social`). Images via `/rest/images?action=initializeUpload` → PUT → URN; carousels as **MultiImage** posts; video via `/rest/videos`. Author = org URN. Tokens: access 60 days, refresh ~1 year. **If a refresh token is present, refresh automatically** and print the new tokens for updating secrets (or update via `gh secret set` when available); otherwise warn 7 days before expiry.

All adapters: backoff on 429/5xx (max 3 attempts), no retry on 4xx, all HTTP logged to stdout with tokens redacted.

## 7. Secrets

Flat, one secret per value (GitHub advises against structured secrets; it also lets one token be rotated without touching the others):

```
GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
META_APP_ID, META_APP_SECRET                       # for auth meta
LINKEDIN_CLIENT_ID, LINKEDIN_CLIENT_SECRET         # for auth linkedin (redirect http://localhost:8765/callback)
META_PAGE_TOKEN_<SLUG>                             # per brand
LINKEDIN_ACCESS_TOKEN_<SLUG>, LINKEDIN_REFRESH_TOKEN_<SLUG>, LINKEDIN_TOKEN_EXPIRES_<SLUG>
```

`<SLUG>` = brand slug upper-cased, `-` → `_`. Locally in `.env` (gitignored); `.env.example` documents each. `postpilot doctor` validates all of them: Sheet readable/writable, each Drive folder readable, each page/org reachable with its token, token expiry, R2 write + public-read via the custom domain, lifecycle rule present, Telegram send test.

## 8. CLI

| command | purpose |
|---|---|
| `postpilot init` | write `config.yaml`, `.env.example`, folders |
| `postpilot sheet init [--brand slug]` | create/repair tabs, headers, dropdowns, protections, hide `_State` |
| `postpilot sync` | validate rows, assign IDs, hashes, `_State` rows; write `scheduled`/`invalid` + Error |
| `postpilot prepare [--post id]` | download + normalise + upload media for due/upcoming posts |
| `postpilot publish [--dry-run] [--live --confirm] [--brand slug] [--post id]` | the run algorithm in §4; local live requires both flags |
| `postpilot status [--brand slug]` | rich table of upcoming / published / failed / needs-review |
| `postpilot summary` | send the Telegram daily summary |
| `postpilot doctor` | full environment check (§7) |
| `postpilot auth linkedin --brand slug` / `auth meta --brand slug` | guided token acquisition |

`--dry-run` goes through the entire pipeline (Sheet read, media normalise, R2 upload, lease **not** written) and prints exactly what each API call would send.

## 9. GitHub Actions

- `publish.yml`: `schedule: "7,22,37,52 * * * *"` + `workflow_dispatch`. Steps: checkout → `uv sync` (cached) → `postpilot publish --live --confirm`. Job timeout 10 min. No git commits from this workflow.
- `summary.yml`: `"0 4 * * *"` (09:00 PKT) → `postpilot summary`.
- `keepalive.yml`: monthly empty commit so scheduled workflows are never auto-disabled.
- Logs: stdout only on runners (plus `_Log`); a local file log only when running on my machine. Upload a redacted log as a workflow artifact on failure.
- `scripts/install-local-launchd.sh` for a Mac alternative (runs `publish --live --confirm`); document that only one of the two should be active.

## 10. Failure-injection tests (must pass before any real API is wired in)

Simulate, with fakes, and assert `_State` ends up correct and nothing would be sent twice:

```
crash before the API request              → retried next run
crash after 2xx, before _State write      → unknown → reconciled or needs_review, never re-sent
Sheet write fails after FB success        → unknown, IG/LI unaffected
IG succeeds, LI returns 429               → partial; LI retried with backoff
runner dies during ffmpeg                 → lease expires → unknown → reconciliation finds nothing → scheduled again (media never left the machine)
token expired mid-run                     → permanent_failed with clear error; summary warns
same workflow starts twice                → second run skips leased rows
user edits caption while publishing       → published version hash recorded; Notes warning
user fixes invalid media name             → invalid → scheduled automatically
user sets Action=retry on failed row      → scheduled, attempts reset, Action cleared
```

Plus unit tests for row parsing, timezone/due selection, media policies (ffprobe fixtures), and each adapter against recorded HTTP fixtures (`respx`). No test touches a real API or Google.

## 11. Engineering standards

Pydantic models for everything crossing a boundary; defensive Sheet parsing (whitespace, `TRUE`/`yes`, blank rows, reordered rows — always look rows up by `ID`, never by position). Structured logging. `docs/DECISIONS.md` with one paragraph per design decision explaining *why*. `README.md` (developer, 10-minute setup: service account → share Sheet/Drive → R2 bucket + custom domain + lifecycle rule → Meta app + page tokens → LinkedIn app + Community Management API access → Telegram bot → `doctor` → push → GitHub secrets).

## 12. Non-goals for v1

No web UI, no AI caption generation, no analytics, no stories, no comment handling, no post editing/deleting after publish, no multi-user permissions.

## 13. Phases — stop after each and demo

0. **API access spike** (throwaway scripts, half a day): confirm FB test-page publish, IG Business publish via container flow, **LinkedIn Community Management API access + `w_organization_social`** (if not approved, tell me and we build FB/IG first), R2 custom-domain public read, service-account access to my Sheet/Drive. Record the exact current media limits in `docs/MEDIA_POLICIES.md`.
1. **Models + Sheet**: config, `init`, `sheet init`, `sync`, `status`, `_State`/`_Log` handling, tests. Reads/writes my real Sheet.
2. **Drive + media + R2**: policies, normalisation, deterministic keys, `prepare`, `doctor` checks.
3. **State machine + dry-run**: lease, hashes, Action handling, reconciliation hooks, all failure-injection tests green with fake publishers.
4. **Facebook** adapter + reconciliation + one real image post to a test brand.
5. **Instagram** adapter (image, carousel, reel) incl. publishing-limit check and container reconciliation.
6. **LinkedIn** adapter + `auth linkedin` + token refresh.
7. **Automation**: workflows, Telegram summary, launchd script, docs polish.

Before writing code, ask me only for: the Sheet ID, the brand names/slugs to seed in `_Brands`, and the custom domain to use for R2. Then start Phase 0.
