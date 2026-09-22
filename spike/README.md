# Phase 0 — access spike

**Throwaway code.** These scripts answer one question — *do we actually have
the access we think we have?* — before a line of the real tool is written.
Nothing in `postpilot/` may import from here.

They are read-only by default. The only script that can publish is
`check_meta.py`, and only when given `--publish` explicitly.

## Run

```bash
uv sync
cp .env.example .env     # fill in whatever you have; blanks report as SKIP
uv run python spike/run_all.py
```

A credential you have not set yet reports **SKIP**, not an error — the exit
code reflects real failures only, so you can fill `.env` in over several days
and keep re-running this.

| script | proves |
|---|---|
| `run_all.py` | everything below, each in its own subprocess, plus `ffmpeg`/`ffprobe` |
| `check_google.py` | credentials parse; Sheet readable **and writable**; each brand's Drive folder readable, including Shared Drives; `md5Checksum` present on every usable file |
| `check_r2.py` | bucket reachable; PUT + HEAD; **anonymous public GET over the custom domain**; 60-day lifecycle rule present |
| `check_meta.py` | Page token valid + scopes + expiry; Page reachable with `CREATE_CONTENT`; IG Business account linked and matching `_Brands`; IG 24-hour publishing quota |

LinkedIn is **not** checked — API access is pending. See `CLAUDE.md` §0.2.

## Individual runs

```bash
uv run python spike/check_google.py
uv run python spike/check_r2.py
uv run python spike/check_meta.py --brand acme-realty
uv run python spike/check_meta.py --brand acme-realty --ig-id 17841400000000000
```

Before `_Brands` exists, `check_google.py` will test a single Drive folder if
you set `DRIVE_FOLDER_ID` in `.env`.

Set `SPIKE_TRACEBACK=1` to see full tracebacks instead of one-line failures.

## Publishing for real

This posts to a live audience. Use a throwaway brand.

```bash
uv run python spike/check_meta.py --brand test-brand --publish \
    --image-url https://media.example.com/postpilot/_spike/test.jpg \
    --platform both
```

The image URL must be **publicly reachable** — Meta fetches it from its own
servers, which is the same reason `check_r2.py` tests an anonymous GET.
`--platform fb|ig|both` limits the blast radius while you are iterating.

## What the checks are guarding against

Each one exists because its failure is invisible until the worst moment:

- **anonymous R2 GET** — a bucket you can write to but the public cannot read
  fails only at publish time, on a post that was supposed to go out.
- **IG User ID vs `_Brands`** — a copy-pasted ID publishes one brand's content
  to another brand's account. v1 cannot delete posts.
- **Drive `md5Checksum`** — the R2 key depends on it; without it, media is
  re-downloaded and re-normalised on every single run.
- **IG publishing quota** — hitting the 24-hour cap burns a retry attempt on
  something that was never going to work.
- **token scopes** — a missing scope fails at 3am, on a Saturday, silently.
