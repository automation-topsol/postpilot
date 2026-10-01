# The weekly import

Make the week's material wherever you like, write one plan file, drop both
into PostPilot, check the preview, click **Import**. That's the whole job.

Use the **hosted importer** — the web app URL bookmarked from the README's
"Hosted importer" setup. It opens in the browser, only for your Google
account, with nothing to run. The local version still works the same way:

```bash
uv run postpilot ui          # opens http://127.0.0.1:8766
```

The two differ only in small ways: on the hosted page, **Show the plan
template** displays the template to copy or download, and there is no
**Sign in with Google** button — you are already signed in.

---

## 1. Write the plan

One Markdown (`.md`) file for the whole week. One block per post; blocks are
separated by a line containing only `---`.

```markdown
brand: grandinvitation          <- optional: default brand for every post

## Mon 2026-09-28 18:30
platforms: FB, IG
type: carousel
media: gold-1.png, gold-2.png, gold-3.png
link: https://grandinvitation.com/gold

caption:
Our new Gold collection is here.
Three designs, one feeling: timeless.

caption ig:
Our new Gold collection is here ✨
#weddinginvitations #goldfoil

---

## Wed 2026-09-30 12:00
brand: restocklypos
platforms: LI
type: text

caption:
Stock counts in half the time.
```

| Line | Meaning |
|---|---|
| `## ... YYYY-MM-DD HH:MM` | Starts a post. The date and time are **Pakistan time**. `6:30 PM` works too. The day name is optional — if it disagrees with the date, the preview warns and the **date wins**. |
| `brand:` | The brand's **slug** from the `_Brands` tab. One `brand:` line above the first post sets a default. |
| `platforms:` | Any of `FB, IG, LI`. |
| `type:` | `image` (1 image) · `carousel` (2–10 images) · `reel` (1 video) · `text` (no media — not possible on Instagram). |
| `media:` | File names **exactly as the files you drop**, comma-separated, in carousel order. |
| `link:` | Optional. Added to Facebook and LinkedIn captions; Instagram ignores it. |
| `caption:` | The text for every platform. Can run over many lines. |
| `caption fb:` / `caption ig:` / `caption li:` | Optional per-platform text. The brand's default hashtags are added to Instagram only when its caption has no `#` of its own. |

**Put the fields first and the captions last.** Once a caption starts,
everything until the next caption, `---` or `##` belongs to it — so a caption
line that happens to begin with "link:" stays part of the caption.

### Let an AI write it

On the importer's start page, **Copy a prompt for your AI** puts the exact
rules, your real brand slugs and an example on the clipboard. Paste it into
ChatGPT, Claude, Gemini or whatever you used for the content, add what you want
posted this week and the file names, and save the answer as `plan.md`.
`Download the plan template` gives you a filled-in example to start from.

---

## 2. Drop it in

Drag the `.md` plan **and every image and video it mentions** onto the page,
all at once (or use **Choose files…**). Forgot one? **Add missing files…**
adds it without starting over.

## 3. Check the preview

Every post becomes a card showing its thumbnails, its time, and the **exact
caption each platform will get** (click `FB` / `IG` / `LI`).

| Badge | Meaning |
|---|---|
| **ready** | Will be imported. |
| **partly ready** | Imports, but one platform can't take it (e.g. a text post on IG). The other platforms still publish. |
| **needs fixing** | Won't be imported until you fix what's in red. |
| **already scheduled** | The same post (brand + time + captions) is already in the Sheet. It is skipped, so importing the same plan twice never doubles anything. |

You can edit any field on the card, or untick **include** to leave a post out.
A post whose time is **in the past** is blocked on purpose — a plan imported
late must not publish a whole week at once. Change the time, or set it a few
minutes ahead to post on the next run.

## 4. Import

Click **Import**. The first time, **Sign in with Google** first — uploads go
into each brand's Drive folder as you.

- A file that is already in the folder (same bytes) is **reused**, not
  uploaded again.
- A new file whose name is already taken is uploaded as `name-1a2b3c.png`, so
  the Sheet never sees two files with one name.
- Rows are added to each brand's tab with **ID blank**. The next scheduled run
  (within 15 minutes) assigns IDs and checks everything, and from then on the
  posts behave exactly like hand-typed rows: they show in the Sheet's `Status`
  column, publish on time, and appear in the 09:00 email.

The importer never publishes anything itself. If you need to change a post
after importing it, edit its row in the Sheet as usual.
