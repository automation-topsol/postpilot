# How to schedule a post

You need two things: **the Google Sheet** and **your brand's Google Drive
folder**. That's it — no apps to install, nothing to click "publish" on.

---

## The short version

1. Put your image or video in the brand's **Drive folder**.
2. Open the Sheet and go to your brand's **tab** (the tab name is the brand).
3. Fill in one row: **Date, Time, Platforms, Type, Media, Caption**.
4. Leave everything else blank. Close the Sheet.

That's the whole job. The post goes out on its own.

> **Timing:** posts go out **within 15–30 minutes** of the time you put in the
> Time column. If you need something live at exactly 6:00 pm, schedule it for
> 5:30 pm. There is no way to make it more precise, and that's normal.

---

## The columns you fill in

| Column | What to put | Example |
|---|---|---|
| **Date** | The day it should go out. **Use `YYYY-MM-DD`.** | `2026-10-07` |
| **Time** | 24-hour clock, your local time. | `18:30` |
| **Platforms** | Where it goes, separated by commas. | `FB, IG` |
| **Type** | Pick from the dropdown. | `image` |
| **Media** | The file name(s) from the Drive folder. | `sunset-invite.png` |
| **Caption** | What the post says. | `Every love story…` |
| Caption (Facebook) | *Optional.* Use only if Facebook needs different words. | |
| Caption (Instagram) | *Optional.* Same idea. Hashtags usually go here. | |
| Caption (LinkedIn) | *Optional.* Same idea. | |
| **Link** | *Optional.* A web address to add to the caption. | `https://…` |
| **Action** | Leave blank. Only used to fix a problem — see below. | |

**Leave `ID` blank.** It fills itself in the first time the system sees your
row (something like `gi-0007`). Once it appears, **don't change or delete it** —
it's how the system remembers this exact post.

### Platforms

Write `FB` for Facebook, `IG` for Instagram, `LI` for LinkedIn. Combine them
with commas: `FB, IG`.

You can only use platforms your brand actually has. If you're not sure, the
brand's row in the `_Brands` tab lists them under *Enabled Platforms*.

### Type, and how many files each one needs

This is strict — the wrong number of files stops the post.

| Type | Files in the Media column |
|---|---|
| `image` | **exactly 1** image |
| `carousel` | **2 to 10** images |
| `reel` | **exactly 1** video |
| `text` | **none** — leave Media empty |

A `text` post works on **Facebook and LinkedIn only**. Instagram always needs
a picture or video. If you put `FB, IG` on a text post, Facebook will publish
it and Instagram will be skipped — the rest of the row still works.

### Media — naming your files

Type the **file name exactly as it appears in Drive**, including the `.png` or
`.jpg` at the end. For a carousel, separate the names with commas — **the order
you type them is the order they appear in the post**.

```
sunset-invite.png
invite-1.png, invite-2.png, invite-3.png
```

Two rules worth knowing:

- **Don't give two files in the folder the same name.** If a name matches more
  than one file, the system stops and says *"ambiguous"* rather than guessing.
  It will never pick one at random.
- **Google Docs, Slides and Sheets don't work** as media. Only real image and
  video files.

You can also paste a Drive share link instead of a file name, if that's easier.

### Sizes — you usually don't need to worry

Pictures and videos are **adjusted automatically** to fit each platform:
converted to the right format, resized, and padded if the shape is wrong.
When something is changed, it's noted in that row's **Notes** column.

The only hard limits worth remembering:

- **Videos for Facebook must be 90 seconds or shorter.** Instagram allows much
  longer, so a long video can work on Instagram but not on Facebook.
- Very small pictures (under 320 pixels wide) will look stretched.

Anything that genuinely can't be fixed shows up as a clear message in the
**Error** column.

### Captions

Put your words in **Caption** and they're used everywhere. Use the
per-platform caption columns only when one platform needs something different
— an Instagram caption with hashtags, say, while Facebook stays clean.

Two things happen automatically:

- **Link** is added to the end of Facebook and LinkedIn captions.
  **Instagram ignores it**, because links aren't clickable in Instagram
  captions anyway.
- The brand's standard hashtags are added to **Instagram only**, and only if
  you haven't written any hashtags of your own. Yours always win.

---

## Reading the Status column

You don't fill this in — it tells you what's happening.

| Status | What it means | Do you need to do anything? |
|---|---|---|
| `draft` | No date yet. Nothing will happen. | Add a Date when ready. |
| `scheduled` | All good, waiting for its time. | **No.** |
| `publishing` | Going out right now. | No — wait a few minutes. |
| `published` | It's live. Links are in *Published URLs*. | No. |
| `partial` | Some platforms worked, others didn't. | **Yes** — see *Error*. |
| `invalid` | Something in the row is wrong. | **Yes** — see *Error*. |
| `failed` | It tried and couldn't. | **Yes** — see *Error*. |
| `needs_review` | It's genuinely unclear whether it posted. | **Yes** — read on. |

### About `needs_review`

This means something went wrong at the exact moment of posting and the system
**cannot tell** whether the post went out. Rather than risk posting the same
thing twice, it stops and asks you.

**Go and look at the actual Facebook page or Instagram account.**

- **It's there** → set **Action** to `mark published`.
- **It's not there** → set **Action** to `retry`.

This is the one situation where the system needs a human, and it's deliberate.
Posting twice can't be undone; waiting for you can.

---

## Fixing things — the Action column

You have three options in the dropdown:

| Action | Use it when |
|---|---|
| `retry` | You've fixed the problem and want it to try again. |
| `mark published` | It actually went out, but the Sheet doesn't know. |
| `skip` | You've changed your mind. Don't post it. |

Pick one, then leave it. The system does it and **clears the cell**, so a blank
Action just means it's been handled.

### Most problems fix themselves

**If the Error column tells you what's wrong, just fix the row.** Change the
file name, add the missing picture, correct the date — and the post goes back
to `scheduled` on its own within about half an hour. You only need `retry` when
the row was already correct and something else failed.

### One thing to know about editing

**Once a post says `published`, editing the row does not change it.** The post
is already live. Changing the caption in the Sheet just adds a note saying the
Sheet and the live post are different. To change a live post, edit it on
Facebook or Instagram directly.

---

## Quick answers

**Can I schedule something for the past?**
Yes — it goes out at the next check, within 15–30 minutes.

**Can I delete a row?**
Yes, if it hasn't published. Deleting a published row does **not** remove the
live post.

**Can I reorder rows?**
Yes. The system tracks posts by their **ID**, not their position. Sort and
rearrange as much as you like.

**I put the wrong date and it already went out.**
It can't be unposted. Delete it on the platform itself.

**Two people editing at once?**
Fine. Just don't both edit the same row at the same moment.

**Nothing happened at all.**
Check the row has a **Date**, a **Type**, and the right number of files in
**Media**, and that **Status** isn't `draft`. If Status is `scheduled` and the
time has passed by more than an hour, tell whoever looks after the system.
