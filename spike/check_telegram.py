#!/usr/bin/env python3
"""Phase 0 spike — Telegram notifier (OPTIONAL).

Telegram is not set up yet, and the tool must work without it. Absence is a
**SKIP** here and a **WARN** in `doctor` — never a failure — because a missing
notifier degrades reporting, not delivery. When it is unconfigured,
`postpilot summary` writes the digest to the `_Log` tab instead, so the daily
summary is never silently lost. See CLAUDE.md §0.3.

Run:  uv run python spike/check_telegram.py
"""

from __future__ import annotations

from _common import Report, load_env, redact, require

API = "https://api.telegram.org"


def main() -> int:
    load_env()
    report = Report("Telegram — daily summary (optional)")
    report.header()

    present, missing = require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    if missing:
        report.skip(
            "Telegram not configured",
            f"unset: {', '.join(missing)} — this is fine. `summary` will write "
            "the daily digest to the _Log tab instead, and `doctor` will warn.",
        )
        return report.summary()

    import httpx

    token, chat_id = present["TELEGRAM_BOT_TOKEN"], present["TELEGRAM_CHAT_ID"]
    report.ok("Telegram configured", "bot token present", value=redact(token), chat_id=chat_id)

    with report.guard("bot token valid"):
        me = httpx.get(f"{API}/bot{token}/getMe", timeout=20).json()
        if not me.get("ok"):
            report.fail("bot token valid", str(me.get("description", me)))
            return report.summary()
        bot = me["result"]
        report.ok("bot token valid", f"@{bot.get('username', '?')}", bot_id=str(bot.get("id", "?")))

    with report.guard("send test message"):
        # Proves the bot can reach *this* chat, which is the part that actually
        # fails in practice: a valid bot that was never added to the group, or
        # a chat_id copied with the wrong sign for a supergroup.
        sent = httpx.post(
            f"{API}/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "PostPilot Phase 0 spike — Telegram works. Please ignore."},
            timeout=20,
        ).json()
        if sent.get("ok"):
            report.ok("send test message", "delivered", message_id=str(sent["result"]["message_id"]))
        else:
            report.fail(
                "send test message",
                f"{sent.get('description', sent)} — check the bot was added to the chat "
                "and that TELEGRAM_CHAT_ID is right (supergroup IDs start with -100)",
            )

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
