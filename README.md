# PostPilot

A Google-Sheet-driven social media scheduler. A non-technical teammate
schedules posts by typing into a Sheet and dropping files in a Drive folder;
PostPilot publishes them to **Facebook Pages, Instagram Business accounts and
LinkedIn Company Pages** for several brands, on a GitHub Actions cron.

No server, no database, no web UI. A CLI, a Sheet, and a scheduled workflow.

> **Delivery guarantee.** PostPilot never knowingly publishes the same post to
> the same platform twice. Once success is recorded for a platform, it is never
> automatically published again. If a remote API result is ambiguous, PostPilot
> stops and asks a human instead of retrying.

**Status: Phase 0 (access spike).** See `CLAUDE.md` §6 for the phase plan.
LinkedIn is deferred until API access is approved; Facebook and Instagram
come first.

---

## Documentation

| File | For |
|---|---|
| `CLAUDE.md` | the design contract — read this first |
| `docs/MEDIA_POLICIES.md` | verified per-platform media limits + pinned API versions |
| `docs/DECISIONS.md` | why each design decision was made |
| `docs/HOW_TO_ADD_A_POST.md` | the non-technical teammate's guide *(Phase 1)* |
| `CLAUDE_CODE_PROMPT.md` | the original full brief |

---

## 10-minute developer setup

### 0. Prerequisites

```bash
brew install uv ffmpeg          # ffmpeg brings ffprobe
git clone <this repo> && cd postpilot
uv sync
cp .env.example .env            # then fill it in as you go through the steps below
```

`ffmpeg`/`ffprobe` are preinstalled on `ubuntu-latest`, so they are a local-only
install. `postpilot doctor` checks for them.

### 1. Google service account

1. In Google Cloud Console: create a project → enable the **Google Sheets API**
   and the **Google Drive API**.
2. Create a **service account**, then a **JSON key**. Note its `client_email`.
3. Put the whole JSON, on one line, in `GOOGLE_SERVICE_ACCOUNT_JSON`.
   *(Locally you can instead point `GOOGLE_SERVICE_ACCOUNT_FILE` at the file.)*

Service accounts **cannot own Drive files**, so you create the Sheet and folders
yourself and share them:

4. Create the Sheet → **share it with the `client_email` as Editor** →
   put its ID (the long string in the URL) in `SHEET_ID`.
5. Create **one Drive folder per brand** → share each with the `client_email`
   as Viewer → put each folder ID in the `_Brands` tab.

### 2. Cloudflare R2

Meta and LinkedIn fetch media **from their own servers**, so the bucket must be
publicly readable over HTTPS.

1. Create an R2 bucket → `R2_BUCKET`, and note the account ID → `R2_ACCOUNT_ID`.
2. Create an **API token** with object read/write on that bucket →
   `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.
3. Attach a **custom domain** (e.g. `media.example.com`) →
   `R2_PUBLIC_BASE_URL=https://media.example.com`.
   An `*.r2.dev` URL works for local testing only — it is rate-limited and not
   meant for production.
4. Add a **lifecycle rule expiring objects after 60 days.** This is what keeps
   R2 inside the free tier at 20–50 posts/week.

`spike/check_r2.py` verifies all four, including an *anonymous* GET over the
custom domain — the one failure mode that would otherwise only appear at
publish time.

### 3. Meta app + Page tokens (Facebook & Instagram)

1. Create a Meta app (Business type) → `META_APP_ID`, `META_APP_SECRET`.
2. Add permissions: `pages_show_list`, `pages_read_engagement`,
   `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`,
   `business_management`.
3. For each brand, mint a **long-lived Page Access Token** →
   `META_PAGE_TOKEN_<SLUG>` (slug upper-cased, `-` → `_`).
   `postpilot auth meta --brand <slug>` walks through this *(Phase 4)*.
4. Each Instagram Business account must be **linked to its Facebook Page** —
   the same Page token then covers both.

### 4. LinkedIn — *pending, Phase 6*

Requires **Community Management API** access and the `w_organization_social`
scope, with the token holder an **ADMIN** of each Company Page.

One token covers **every** page you administer, so the secrets are flat and
unsuffixed — `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_REFRESH_TOKEN`,
`LINKEDIN_TOKEN_EXPIRES`. Only the **org URN** is per brand, in `_Brands`.
Access tokens last ~60 days and refresh tokens ~1 year; `postpilot auth linkedin`
refreshes automatically and prints the new values.

### 5. Telegram

Create a bot with `@BotFather` → `TELEGRAM_BOT_TOKEN`; get the chat ID for
wherever the summary should land → `TELEGRAM_CHAT_ID`. One digest a day at
09:00 Asia/Karachi. No per-post pings.

### 6. Verify, then go

```bash
uv run python spike/run_all.py        # Phase 0 access spike
postpilot doctor                      # full environment check (Phase 2)
postpilot sheet init                  # create/repair the Sheet tabs (Phase 1)
```

Finally, push the repo and add every value in `.env` as a **GitHub Actions
repository secret**, one secret per value.

---

## Running it

```bash
postpilot sync                        # validate rows, assign IDs, write _State
postpilot status                      # what is upcoming / failed / needs review
postpilot publish --dry-run           # full pipeline, prints every API call, sends nothing
postpilot publish --live --confirm    # local live publish — both flags required
postpilot summary                     # send the Telegram digest
```

GitHub Actions is the only routine live publisher; the cron runs at
`7,22,37,52 * * * *`, so posts go out **within 15–30 minutes** of their
scheduled time.

### Which flags exist, and why two of them

`--dry-run` runs everything — Sheet read, media normalisation, R2 upload — and
prints exactly what each API call *would* send, but never writes the lease, so
nothing can be published. Local live publishing needs **both** `--live` and
`--confirm`, because the whole point of the state machine is that exactly one
scheduler is in charge. If you also run the launchd job
(`scripts/install-local-launchd.sh`), disable the GitHub Actions cron — run
one or the other, never both.

---

## Repo layout

```
postpilot/    the tool (built from Phase 1)
spike/        Phase 0 throwaway access checks — never imported by postpilot/
tests/        pytest; fakes and respx fixtures only, no real APIs
docs/         decisions, media policies, the non-technical guide
scripts/      launchd installer for the Mac alternative to Actions
.github/      publish / summary / keepalive workflows (Phase 7)
```

This repo holds **code and docs only** — never content, never secrets.
