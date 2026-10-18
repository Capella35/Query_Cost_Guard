#!/bin/sh
# Install (or with "uninstall", remove) the daily registry refresh for this
# user. Writes ~/Library/LaunchAgents/com.costguard.registry-refresh.plist.
#   sh scripts/schedule/install_launchd.sh            # install + load
#   sh scripts/schedule/install_launchd.sh uninstall  # unload + remove
#   launchctl kickstart gui/$(id -u)/com.costguard.registry-refresh   # run now
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL=com.costguard.registry-refresh
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ "${1:-}" = "uninstall" ]; then
    launchctl bootout "gui/$(id -u)" "$DEST" 2>/dev/null || true
    rm -f "$DEST"
    echo "removed $LABEL"
    exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/runs"
sed "s#REPO#$REPO#g" "$REPO/scripts/schedule/$LABEL.plist" > "$DEST"
launchctl bootout "gui/$(id -u)" "$DEST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "installed $LABEL: daily at 06:00 -> $REPO/runs/registry_refresh.log"
