#!/usr/bin/env bash
# Stop and remove the contract-bus LaunchAgent. Leaves bus.sqlite3 untouched.
set -euo pipefail

LABEL="com.blocksurvey.contract-bus"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
GUI="gui/$(id -u)"

launchctl bootout "$GUI/$LABEL" 2>/dev/null && echo "stopped $LABEL" || echo "$LABEL not loaded"
rm -f "$PLIST" && echo "removed $PLIST"
