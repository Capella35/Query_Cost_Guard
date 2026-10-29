#!/bin/zsh
# Install (or with `uninstall`, remove) the monthly registry-refresh job:
# launchd runs scripts/schedule/refresh_registry.py on the 1st at 09:00.
# Whether it really refreshes is DRY_RUN in that file (True = plan only).
set -euo pipefail
ROOT=${0:A:h:h:h}
LABEL=com.querypreflight.registry-refresh
PLIST=~/Library/LaunchAgents/$LABEL.plist

launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true
if [[ ${1:-} == uninstall ]]; then
  rm -f $PLIST
  echo "removed $LABEL"
  exit 0
fi
mkdir -p ~/Library/LaunchAgents "$ROOT/runs"
cat > $PLIST <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>$ROOT/scripts/schedule/refresh_registry.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <dict><key>Day</key><integer>1</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardErrorPath</key><string>$ROOT/runs/registry_refresh.launchd.err</string>
</dict>
</plist>
PLIST
launchctl bootstrap gui/$(id -u) $PLIST
echo "installed $LABEL: 1st of each month, 09:00 (log: runs/registry_refresh.log)"
echo "run it now: launchctl kickstart gui/$(id -u)/$LABEL"
