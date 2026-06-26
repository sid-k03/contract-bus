"""Idle-wake canary + Stop-supervisor matrix.

The canary proves the property the model-owned idle-wake depends on: a backgrounded watcher's
completion (with its payload) is observable to a process that did NOT block on it. In
production the agent's harness plays that external-observer role (a `task-notification`). If
this canary ever FAILS after a Claude Code upgrade, idle-wake via the watcher is at risk —
fall back to `wait_for_message` as primary (spec §6.1).
"""
import os
import subprocess
import threading
import time

import bus_cli as c

HERE = os.path.dirname(os.path.abspath(__file__))


def _boot(tmp_path, port):
    import bus_server as b
    b.PORT = port; b.DB = str(tmp_path / "e2e.sqlite3"); b._init(b.DB)
    threading.Thread(target=lambda: b.mcp.run(transport="http", host="127.0.0.1", port=port),
                     daemon=True).start()
    time.sleep(2.5)
    return b


def test_backgrounded_watcher_exit_is_externally_observable(tmp_path):
    b = _boot(tmp_path, 9134)
    state = str(tmp_path / "state")
    env = dict(os.environ, CONTRACT_BUS_BASE="http://127.0.0.1:9134", CONTRACT_BUS_STATE=state)
    proc = subprocess.Popen(["bash", os.path.join(HERE, "bus_watch.sh"), "obs", "obs-1", "0"],
                            stdout=subprocess.PIPE, text=True, env=env)
    # observer does NOT block on proc; it does other work, then mail arrives
    time.sleep(0.4)
    assert proc.poll() is None                       # still parked (idle)
    b._post_message(b.DB, "c", "peer", "wake up", recipient="obs-1")
    out, _ = proc.communicate(timeout=10)            # now observable
    assert "wake up" in out and "CURSOR=" in out
    assert proc.returncode == 0


def test_stop_supervisor_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(c, "daemon_up", lambda *a, **k: True)
    monkeypatch.setattr(c, "watcher_alive", lambda *a, **k: False)
    sid = "m"; d = c.state_dir(sid, root=str(tmp_path)); os.makedirs(d)
    open(os.path.join(d, "active"), "w").close()
    with open(os.path.join(d, "identity"), "w") as f:
        f.write("h-m")
    assert c.ev_stop({"session_id": sid, "stop_hook_active": False}) is not None   # fires (dead+clear)
    assert c.ev_stop({"session_id": sid, "stop_hook_active": False}) is None        # throttled
    assert c.ev_stop({"session_id": sid, "stop_hook_active": True}) is None         # loop guard
