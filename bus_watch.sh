#!/bin/sh
# Re-arming-by-the-model ambient watcher (spec §6.1). Writes its own pid+start-time so the
# Stop supervisor (bus_cli.ev_stop) can tell whether a watcher is live. Does ONE long-poll on
# /wait for mail addressed to <handle> (any channel) starting after <since_id>, then exits
# printing the JSON + a final "CURSOR=<maxid>" line the model threads into its next launch.
# The model launches this as a BACKGROUND task — only an agent-launched task's completion
# wakes an idle session. Base URL / state dir overridable for tests.
sid="$1"; handle="$2"; since="${3:-0}"
base="${CONTRACT_BUS_BASE:-http://127.0.0.1:9100}"
root="${CONTRACT_BUS_STATE:-$HOME/.contract-bus}"
d="$root/$sid"; mkdir -p "$d"
printf '%s %s' "$$" "$(ps -o lstart= -p $$ | tr -s ' ')" > "$d/watcher.pid"
trap 'rm -f "$d/watcher.pid"' EXIT
resp="$(curl -s --max-time 610 "$base/wait?as_handle=$handle&since_id=$since&timeout=600")"
printf '%s\n' "$resp"
maxid="$(printf '%s' "$resp" | python3 -c "import sys,json;d=json.load(sys.stdin);print(max((m['id'] for m in d.get('messages',[])), default=$since))" 2>/dev/null || echo "$since")"
# Own the cursor FILE (not just stdout): a hook-injected re-arm directive reads since_id from
# this file (bus_cli.read_cursor), so it must be current or old mail re-delivers. Atomic write.
printf '%s' "$maxid" > "$d/cursor.tmp" && mv "$d/cursor.tmp" "$d/cursor"
printf 'CURSOR=%s\n' "$maxid"
