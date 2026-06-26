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


# --- cursor + watcher liveness (pid + start-time, defeats PID reuse) -------

def read_cursor(session_id: str, root: str = STATE_ROOT) -> int:
    try:
        with open(os.path.join(state_dir(session_id, root), "cursor")) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_cursor(session_id: str, value: int, root: str = STATE_ROOT) -> None:
    d = state_dir(session_id, root)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "cursor.tmp")
    with open(tmp, "w") as f:
        f.write(str(int(value)))
    os.replace(tmp, os.path.join(d, "cursor"))


def _norm(s: str) -> str:
    """Collapse runs of whitespace so `ps -o lstart=` output compares stably regardless of
    the column padding the shell (bus_watch.sh) or Python sees."""
    return " ".join(s.split())


def _proc_starttime(pid: int) -> str | None:
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    s = out.stdout.strip()
    return s or None


def write_watcher_pid(session_id: str, pid: int, starttime: str, root: str = STATE_ROOT) -> None:
    d = state_dir(session_id, root)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "watcher.pid"), "w") as f:
        f.write(f"{pid} {starttime}")


def watcher_alive(session_id: str, root: str = STATE_ROOT) -> bool:
    try:
        with open(os.path.join(state_dir(session_id, root), "watcher.pid")) as f:
            raw = f.read().strip()
    except OSError:
        return False
    if not raw:
        return False
    pid_s, _, recorded_st = raw.partition(" ")
    try:
        pid = int(pid_s)
    except ValueError:
        return False
    current_st = _proc_starttime(pid)
    return current_st is not None and _norm(current_st) == _norm(recorded_st)


# --- daemon liveness + register (graceful degradation; never raises) -------

def daemon_up(timeout: float = CONNECT_TIMEOUT) -> bool:
    """True iff the v2 daemon answers. GET /wait with no args returns 400 on v2."""
    try:
        urllib.request.urlopen(f"{BASE}/wait", timeout=timeout)
        return True  # 200 (shouldn't happen without args) still means it's up
    except urllib.error.HTTPError as e:
        return e.code == 400
    except (urllib.error.URLError, OSError):
        return False


def register(handle: str, repo: str | None = None, status: str = "online",
             current_task: str | None = None, timeout: float = CONNECT_TIMEOUT) -> bool:
    """POST /register. True on 200; never raises (graceful degradation)."""
    fields = {"handle": handle, "status": status}
    if repo is not None:
        fields["repo"] = repo
    if current_task is not None:
        fields["current_task"] = current_task
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{BASE}/register", data=data),
                                    timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False
