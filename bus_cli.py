"""contract-bus hook brain — invoked per Claude Code hook event (see hooks.settings.snippet.json).

Reads the hook's JSON from stdin, gates on an activation marker, and registers presence /
emits a watcher-launch directive / persists the cursor / reaps state. It NEVER spawns the
ambient watcher: only an agent-launched background task wakes an idle session (spec §6.1),
so the watcher is launched BY THE MODEL (bus_watch.sh) per a directive this script injects.

State per session: ~/.contract-bus/<session_id>/{active,identity,cursor,watcher.pid}
Keyed by session_id (always in hook stdin, stable per session); the handle is in `identity`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "127.0.0.1"
PORT = 9100
BASE = f"http://{HOST}:{PORT}"
CONNECT_TIMEOUT = 2.0
STATE_ROOT = os.environ.get("CONTRACT_BUS_STATE", os.path.expanduser("~/.contract-bus"))
STATE_TTL_DAYS = 7


# --- identity / handle / state dir ----------------------------------------

def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return s.strip("-")


def derive_handle(project_root: str, session_id: str) -> str:
    return f"{slug(os.path.basename(project_root.rstrip('/')))}-{session_id[:8]}"


def state_dir(session_id: str, root: str = STATE_ROOT) -> str:
    return os.path.join(root, session_id)


def read_identity(session_id: str, root: str = STATE_ROOT) -> str | None:
    p = os.path.join(state_dir(session_id, root), "identity")
    try:
        with open(p) as f:
            return f.read().strip() or None
    except OSError:
        return None


def is_active(session_id: str, root: str = STATE_ROOT) -> bool:
    return os.path.exists(os.path.join(state_dir(session_id, root), "active"))
