"""Tests for the shell hook scripts (bus_gate.sh, bus_watch.sh) via subprocess.

bus_watch.sh is exercised against a live temp daemon on an alt port (9133), distinct from the
real LaunchAgent on 9100 and the other test ports.
"""
import json
import os
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _boot(tmp_path, port):
    import bus_server as b
    b.PORT = port; b.DB = str(tmp_path / "e2e.sqlite3"); b._init(b.DB)
    threading.Thread(target=lambda: b.mcp.run(transport="http", host="127.0.0.1", port=port),
                     daemon=True).start()
    time.sleep(2.5)
    return b


def test_gate_exits_silently_when_inactive(tmp_path):
    env = dict(os.environ, CONTRACT_BUS_STATE=str(tmp_path))
    payload = json.dumps({"session_id": "sX", "cwd": "/x", "hook_event_name": "Stop"})
    r = subprocess.run(["sh", os.path.join(HERE, "bus_gate.sh")],
                       input=payload, capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert r.stdout.strip() == ""        # no passthrough → caller skips Python


def test_gate_passes_through_when_active(tmp_path):
    sid = "sX"; d = os.path.join(str(tmp_path), sid); os.makedirs(d)
    open(os.path.join(d, "active"), "w").close()
    env = dict(os.environ, CONTRACT_BUS_STATE=str(tmp_path))
    payload = json.dumps({"session_id": sid, "cwd": "/x", "hook_event_name": "Stop"})
    r = subprocess.run(["sh", os.path.join(HERE, "bus_gate.sh")],
                       input=payload, capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert json.loads(r.stdout)["session_id"] == sid   # passthrough → caller runs Python


def test_watch_writes_pid_then_wakes_and_emits_cursor(tmp_path):
    b = _boot(tmp_path, 9133)
    state = str(tmp_path / "state")
    env = dict(os.environ, CONTRACT_BUS_BASE="http://127.0.0.1:9133", CONTRACT_BUS_STATE=state)
    proc = subprocess.Popen(["bash", os.path.join(HERE, "bus_watch.sh"), "sW", "backend-9", "0"],
                            stdout=subprocess.PIPE, text=True, env=env)
    time.sleep(0.6)
    # R1: the watcher writes its pid file while parked, so the Stop supervisor sees it live
    assert os.path.exists(os.path.join(state, "sW", "watcher.pid"))
    b._post_message(b.DB, "bugs", "frontend", "do the thing", recipient="backend-9")
    out, _ = proc.communicate(timeout=10)
    assert "do the thing" in out
    assert "CURSOR=" in out
    cur = int([l for l in out.splitlines() if l.startswith("CURSOR=")][0].split("=")[1])
    assert cur >= 1
    # the watcher OWNS the cursor file (so a hook-injected re-arm resumes at the right id)
    with open(os.path.join(state, "sW", "cursor")) as f:
        assert int(f.read().strip()) == cur
    # trap removed the pid file on exit
    assert not os.path.exists(os.path.join(state, "sW", "watcher.pid"))
