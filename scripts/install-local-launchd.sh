#!/usr/bin/env bash
# Install a launchd job that runs `postpilot publish` on this Mac.
#
# This is an ALTERNATIVE to the GitHub Actions cron, not an addition.
# Run one or the other. Two schedulers is precisely the situation the 20-minute
# lease exists to survive, and surviving it is not the same as wanting it: the
# machines disagree about the clock, each run does redundant work, and every
# ambiguous result costs a human a look at the platform.
#
#   install:    ./scripts/install-local-launchd.sh
#   uninstall:  ./scripts/install-local-launchd.sh --uninstall
#
# Note the Mac must be awake for launchd to fire. If it sleeps at night,
# GitHub Actions is the better choice.

set -euo pipefail

LABEL="ai.postpilot.publish"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO/logs"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $LABEL"
    exit 0
fi

UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
    echo "uv is not on PATH. brew install uv" >&2
    exit 1
fi

if [[ ! -f "$REPO/.env" ]]; then
    echo "No .env in $REPO — copy .env.example and fill it in first." >&2
    exit 1
fi

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"

# Same stagger as the GitHub cron, so switching between them changes nothing
# about when posts go out.
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>WorkingDirectory</key><string>$REPO</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV</string>
        <string>run</string>
        <string>postpilot</string>
        <string>publish</string>
        <string>--live</string>
        <string>--confirm</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Minute</key><integer>7</integer></dict>
        <dict><key>Minute</key><integer>22</integer></dict>
        <dict><key>Minute</key><integer>37</integer></dict>
        <dict><key>Minute</key><integer>52</integer></dict>
    </array>
    <key>StandardOutPath</key><string>$LOG_DIR/publish.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/publish.err</string>
    <key>RunAtLoad</key><false/>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed $LABEL — runs at :07, :22, :37 and :52."
echo "Logs: $LOG_DIR/publish.log"
echo
echo "NOW DISABLE THE GITHUB ACTIONS CRON, or you will be running two"
echo "schedulers: Actions tab -> publish -> ... -> Disable workflow."
