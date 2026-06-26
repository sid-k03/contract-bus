#!/bin/sh
# Fast activation gate. Reads hook JSON on stdin; if this session is NOT opted in, exit 0
# silently (no passthrough) so the caller skips Python. If active, echo stdin back so the
# caller can pipe it to `python3 bus_cli.py <event>`. No git, no Python — keep it cheap;
# this runs on EVERY hook of EVERY session machine-wide.
root="${CONTRACT_BUS_STATE:-$HOME/.contract-bus}"
input="$(cat)"
# extract "session_id":"..." with sed (no jq dependency)
sid="$(printf '%s' "$input" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
[ -n "$sid" ] || exit 0
[ -f "$root/$sid/active" ] || exit 0
printf '%s' "$input"
