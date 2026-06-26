#!/bin/sh
# Merge the contract-bus hook block into ~/.claude/settings.json (user scope), pinning this
# repo's absolute path. Idempotent: re-running replaces the contract-bus hooks. Needs python3.
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
chmod +x "$ROOT/bus_gate.sh" "$ROOT/bus_watch.sh"
python3 - "$ROOT" "$SETTINGS" <<'PY'
import json, os, sys
root, settings = sys.argv[1], sys.argv[2]
with open(os.path.join(root, "hooks.settings.snippet.json")) as f:
    snip = json.load(f)["hooks"]
snip = json.loads(json.dumps(snip).replace("__ROOT__", root))
try:
    with open(settings) as f:
        cur = json.load(f)
except (OSError, ValueError):
    cur = {}
cur.setdefault("hooks", {})
for ev, groups in snip.items():
    cur["hooks"][ev] = groups   # replace contract-bus events wholesale (idempotent)
os.makedirs(os.path.dirname(settings), exist_ok=True)
with open(settings, "w") as f:
    json.dump(cur, f, indent=2)
print(f"wired contract-bus hooks into {settings}")
PY
# Link the 3 skills into the user skills dir so Claude Code auto-discovers them (no plugin
# needed for the interim; Plan 3 ships them inside the plugin instead). Symlinks stay in sync
# with the repo.
SKILLS_DIR="${CLAUDE_SKILLS:-$HOME/.claude/skills}"
mkdir -p "$SKILLS_DIR"
for s in join-contract-bus orchestrating-contract-bus-sessions conclude-bus-session; do
  ln -sfn "$ROOT/skills/$s" "$SKILLS_DIR/$s"
done
echo "Linked skills into $SKILLS_DIR"
echo "Done. New sessions pick up the hooks + skills; existing sessions: restart or /hooks reload."
