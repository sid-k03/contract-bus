#!/usr/bin/env bash
# Install contract-bus as a macOS LaunchAgent: starts at login, respawns on crash.
# Idempotent — re-run to pick up path/config changes.
set -euo pipefail

LABEL="com.blocksurvey.contract-bus"
# Absolute project dir (resolves symlinks, tolerates spaces).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PY="$DIR/.venv/bin/python"
DB="$DIR/bus.sqlite3"
LOG_DIR="$HOME/Library/Logs"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -x "$PY" ]]; then
  echo "error: venv python not found at $PY" >&2
  echo "create it first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PY</string>
        <string>$DIR/bus_server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>CONTRACT_BUS_DB</key>
        <string>$DB</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <!-- Auto-reload is handled IN-PROCESS by bus_server.py (it exits on its own source
         change; KeepAlive respawns it). NOT via launchd WatchPaths: WatchPaths only starts
         a stopped job, and KeepAlive keeps this one always running, so WatchPaths is inert
         here (verified empirically). -->
    <key>StandardOutPath</key>
    <string>$LOG_DIR/$LABEL.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/$LABEL.log</string>
</dict>
</plist>
PLISTEOF

echo "wrote $PLIST"

GUI="gui/$(id -u)"
# Replace any existing instance, then load + start.
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
launchctl bootstrap "$GUI" "$PLIST"
launchctl enable "$GUI/$LABEL"
launchctl kickstart -k "$GUI/$LABEL"

echo "loaded $LABEL"
echo "logs:   $LOG_DIR/$LABEL.log"
echo "status: launchctl print $GUI/$LABEL | grep -E 'state|pid'"
echo "url:    http://127.0.0.1:9100/mcp"
