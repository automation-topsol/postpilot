# Media policies — verified limits

**Verified against official documentation on 2026-09-23.**

Every number below was read from the vendor's own current docs, not from the
starting figures in `CLAUDE_CODE_PROMPT.md` §5 — several of those were wrong,
and the corrections are listed in §6. Each `MediaPolicy` class in
`postpilot/media/policies.py` must match this file exactly, and **this file is
updated first** whenever a limit changes.

**Re-verify before each phase that touches a platform**, and on any
`permanent_failed` that looks like a spec rejection. Vendors change these
quietly and without a changelog entry.

> **`POLICY_VERSION` is currently `v1`.**
> It lives in `postpilot/media/policies.py` and is part of every R2 key:
> `{brand}/{post_id}/{platform}/{policy_version}-{drive_md5}.{ext}`.
> **Bump it to `v2` the moment any normalisation rule below changes**, so
> previously transformed files are never silently reused.

---

## 1. Pinned API versions

| Constant (`postpilot/apis.py`) | Value | Why |
|---|---|---|
| `GRAPH_API_VERSION` | `v25.0` | Released 2026-02-18, available until **2028-07-29**. Latest is v26.0 (2026-07-29), but v25.0 has ~22 months of runway and seven months of production soak. Deliberately not tracking latest. |
| `LINKEDIN_VERSION` | `202609` | LinkedIn `Linkedin-Version` header, `YYYYMM`. Current default moniker. **Note: version `202510` sunsets 2026-10-15** — anything older must be migrated. |

LinkedIn also requires `X-Restli-Protocol-Version: 2.0.0` on every REST call.

---

## 2. Instagram (Business accounts, Content Publishing API)

Source: [IG User `/media` reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media/) ·
[Content Publishing guide](https://developers.facebook.com/docs/instagram-platform/content-publishing/)

### 2.1 Image (`InstagramImagePolicy`)

| Spec | Limit |
|---|---|
| Format | **JPEG only** |
| Max file size | **8 MB** |
| Aspect ratio | **4:5 to 1.91:1** |
| Min width | **320 px** (scaled up if smaller) |
| Max width | **1440 px** (scaled down if larger) |
| Height | varies by aspect ratio |
| Colour space | **sRGB** |

**Normalisation:** convert to JPEG, sRGB. If the aspect ratio is outside
4:5–1.91:1, **pad with a blurred background** to the nearest in-range ratio —
crop only when the overshoot is within 5 %. Resize so the long edge is
≤ 1440 px. Re-encode at descending JPEG quality until ≤ 8 MB.

### 2.2 Carousel (`InstagramCarouselPolicy`)

| Spec | Limit |
|---|---|
| Items | **2–10** (docs: "up to 10 images, videos, or a combination") |
| Per-item specs | identical to §2.1 |
| Aspect ratio | **all items must share one ratio**; docs default to 1:1 |

**Normalisation:** compute the target ratio once from the first item (clamped
into range), then pad every item to that same ratio. Never let two items in one
carousel differ.

### 2.3 Reel (`InstagramReelPolicy`)

| Spec | Limit |
|---|---|
| Container | **MOV or MP4** (MPEG-4 Part 14) |
| Video codec | **HEVC or H.264**, progressive scan, closed GOP, 4:2:0 chroma |
| Audio codec | **AAC**, ≤ 48 kHz sample rate, 1–2 channels |
| Frame rate | **23–60 fps** |
| Max width | **1920 px** |
| Aspect ratio | **0.01:1 to 10:1** (9:16 recommended) |
| Video bitrate | **VBR, 25 Mbps max** |
| Audio bitrate | **128 kbps** |
| Duration | **3 seconds min, 15 minutes max** |
| Max file size | **300 MB** |
| Cover photo | JPEG, ≤ 8 MB, sRGB, 9:16 recommended |

**Normalisation:** `-c:v libx264 -profile:v high -pix_fmt yuv420p -c:a aac
-ar 48000 -b:a 128k -movflags +faststart`. Target 9:16 by padding (never
stretching). Cap frame rate at 30 fps if the source exceeds 60. Reject with a
clear error — do not silently trim — if duration is < 3 s or > 15 min.

### 2.4 Publishing rate limits

| Limit | Value |
|---|---|
| General | **100 API-published posts per rolling 24 hours** |
| Carousels | docs state **50 published posts within a 24-hour period** |
| Check endpoint | `GET /{ig_user_id}/content_publishing_limit` |

`publish` **must call `content_publishing_limit` before attempting an IG post.**
If the quota is exhausted, leave the platform `scheduled` and write the reason
to `Notes` — never burn an attempt on a quota rejection.

> ⚠️ The 50-vs-100 carousel figure needs confirming against live API behaviour
> during the Phase 5 spike. Until then treat **50/24h** as the effective ceiling.

---

## 3. Facebook Pages

### 3.1 Image (`FacebookImagePolicy`)

Source: [Page `/photos` edge](https://developers.facebook.com/docs/graph-api/reference/page/photos/)

| Spec | Limit |
|---|---|
| Formats | **.jpeg, .bmp, .png, .gif, .tiff** |
| Max file size | **10 MB** |
| PNG guidance | docs recommend **≤ 1 MB** for PNG "or the image may appear pixelated" |

**Normalisation:** convert everything to JPEG (avoids the PNG pixelation
caveat entirely), sRGB, re-encode until ≤ 10 MB. No aspect-ratio constraint —
Facebook accepts any ratio, so **do not pad FB images**.

**Endpoint:** `POST /{page-id}/photos` with `url` (our R2 public URL) or a
multipart `source`; `published` defaults to `true`. Carousel: post each photo
with `published=false`, collect the IDs, then `POST /{page-id}/feed` with
`attached_media`.

**Required token scopes:** `pages_show_list`, `pages_read_engagement`,
`pages_manage_posts`. The Page token's holder needs `CREATE_CONTENT`.

**Success response:** `{ "id": ..., "post_id": ... }` — **`post_id` is the one
to record** as `Remote ID`; `id` is the photo object, not the post.

### 3.2 Reel (`FacebookReelPolicy`)

Source: [Reels Publishing API](https://developers.facebook.com/docs/video-api/guides/reels-publishing)

| Spec | Limit |
|---|---|
| Duration | **3 to 90 seconds** |
| Aspect ratio | **9:16** |
| Min resolution | **540 × 960 px** |
| Recommended resolution | **1080 × 1920 px** |
| Video codec | **H.264, H.265** (VP9 and AV1 also supported) |
| Audio codec | **AAC Low Complexity** |
| Frame rate | **24–60 fps** |
| Max file size | not stated in docs |

**This is a genuinely different policy from Instagram Reels** — 90 s vs 15 min,
strict 9:16 vs 0.01:1–10:1, 24 fps floor vs 23. A video that is legal on IG can
be illegal on FB. Keep the two classes separate; never share normalisation
output between them (the R2 key includes `{platform}`, which enforces this).

**Upload flow —** three phases on `POST /{page-id}/video_reels`:
1. `upload_phase: "start"` → returns video ID + upload URL
2. `POST` the bytes to `rupload.facebook.com/video-upload/{video-id}`
3. `upload_phase: "finish"` with `video_state: "PUBLISHED"`

Phase 2 is the point of no return for `unknown` classification: a timeout
*after* the finish call must not be retried.

---

## 4. LinkedIn Company Pages — recorded, not yet exercised

**API access is pending (see `CLAUDE.md` §0.2).** These figures are from the
docs so `LinkedInImagePolicy` / `LinkedInVideoPolicy` can be written in Phase 6
without another research pass. **Nothing here has been confirmed against a live
call.**

### 4.1 Image (`LinkedInImagePolicy`)

Source: [Images API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/images-api)

| Spec | Limit |
|---|---|
| Formats | **JPG, GIF, PNG** |
| Max pixel count | **< 36,152,320 pixels** (width × height) |
| GIF frames | up to **250** |
| Max file size | **not documented** — the constraint is pixel count, not bytes |
| Alt text | max **4,086 chars**, recommended < 120 |

Note this is a **pixel-count** limit, not the byte limit the brief assumed.
36,152,320 px is roughly 6013 × 6013 — very permissive; our IG/FB
normalisation output is far below it.

**Flow:** `POST /rest/images?action=initializeUpload` with
`{"initializeUploadRequest": {"owner": "urn:li:organization:<id>"}}` → returns
`uploadUrl` + `image` URN → PUT the bytes → reference the URN in
`POST /rest/posts`.

**Scopes:** `w_organization_social` for posting. The token holder must have
**ADMIN or DSC** permission on the Company Page. `w_member_social` alone is
write-only and **cannot GET** `/rest/images` — which matters for reconciliation.

### 4.2 Video (`LinkedInVideoPolicy`)

Source: [Videos API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api)

| Spec | Limit |
|---|---|
| Format | **MP4** |
| Duration | **3 seconds to 30 minutes** |
| File size | **75 KB to 500 MB** (feed); `fileSizeBytes` max **5 GB** per the schema |
| Upload | **multipart, 4 MB parts** (`split -b 4194303`) |

**Flow:** `initializeUpload` (declaring `fileSizeBytes`) → PUT each 4 MB part,
**collecting the `ETag` response header of every part** → `finalizeUpload` with
`uploadToken` + `uploadedPartIds` in part order. Upload URLs expire ~30 days
out; an expired URL returns **401**, and `EXPIRED_UPLOAD_URL` is a documented
400. Poll the video's `status` until `AVAILABLE` before referencing it in a
post; `PROCESSING_FAILED` carries `processingFailureReason`.

### 4.3 Carousel

LinkedIn has no direct equivalent of an IG carousel — multi-image posts are
**MultiImage** posts, each image uploaded separately via §4.1 and referenced
together in one `/rest/posts` call.

---

## 5. Cross-platform summary

| | IG image | IG reel | FB image | FB reel | LI image | LI video |
|---|---|---|---|---|---|---|
| Format | JPEG | MP4/MOV | JPEG (converted) | MP4 | JPG/PNG/GIF | MP4 |
| Max size | 8 MB | 300 MB | 10 MB | n/d | 36.15 MP | 500 MB |
| Aspect | 4:5–1.91:1 | 0.01:1–10:1 | any | **9:16 strict** | any | any |
| Duration | — | 3 s–15 min | — | **3–90 s** | — | 3 s–30 min |
| Max width | 1440 px | 1920 px | — | — | — | — |

**The binding constraints in practice:** IG's 8 MB / 1440 px / 4:5–1.91:1 for
images, and FB Reels' 90-second ceiling for video. A post targeting both IG and
FB reels is limited by **FB's 90 s**, and each platform still gets its own
normalised file under its own R2 key.

---

## 6. Corrections to `CLAUDE_CODE_PROMPT.md` §5

The brief said its figures were starting points to be verified. They were:

| Brief said | Actually | Impact |
|---|---|---|
| `FacebookImagePolicy` — JPEG/PNG **≤ 4 MB** | **10 MB**, all of jpeg/bmp/png/gif/tiff | We were being 2.5× stricter than needed |
| `InstagramReelPolicy` — 9:16, **30 fps** | **0.01:1–10:1** accepted, **23–60 fps** | 9:16 is a recommendation, not a requirement; 30 fps is our choice, not a rule |
| `LinkedInImagePolicy` — JPEG/PNG **≤ 8 MB** | **pixel-count** limit (36,152,320 px), no documented byte limit; GIF also allowed | Wrong *kind* of limit |
| IG reel duration "per current docs" | **3 s – 15 min**, ≤ 300 MB | now pinned |
| FB reel duration "different from IG" | **3–90 s** — a 12× tighter ceiling | confirms the brief's instinct to keep the classes separate |

We keep **30 fps** and **9:16** as the IG reel *normalisation target* anyway —
they are what the platform actually favours — but they are now recorded as our
choice, not as a platform requirement, so nobody later "fixes" a non-bug.
