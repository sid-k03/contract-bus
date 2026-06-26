"""Tests for the contract-bus hook brain (bus_cli) — pure helpers against tmp state dirs.

These never invoke the script as a subprocess; they import and call the helpers directly.
State is redirected via the `root=` kwarg (or CONTRACT_BUS_STATE) so nothing touches the
real ~/.contract-bus.
"""
import os

import bus_cli as c


# --- identity / handle / state dir ----------------------------------------

def test_slug_basic():
    assert c.slug("Data Bus MCP") == "data-bus-mcp"


def test_slug_collapses_and_trims():
    assert c.slug("  Foo__Bar//baz  ") == "foo-bar-baz"
    assert c.slug("--Already-Slug--") == "already-slug"


def test_derive_handle_shape():
    h = c.derive_handle("/Users/x/Data Bus MCP", "a3f29c1b9d8e7f6a")
    assert h == "data-bus-mcp-a3f29c1b"


def test_state_dir_under_root(tmp_path):
    d = c.state_dir("sess123", root=str(tmp_path))
    assert d == os.path.join(str(tmp_path), "sess123")


def test_is_active_false_then_true(tmp_path):
    sid = "sess123"
    assert c.is_active(sid, root=str(tmp_path)) is False
    d = c.state_dir(sid, root=str(tmp_path)); os.makedirs(d)
    open(os.path.join(d, "active"), "w").close()
    assert c.is_active(sid, root=str(tmp_path)) is True


def test_read_identity_roundtrip(tmp_path):
    sid = "sess123"
    d = c.state_dir(sid, root=str(tmp_path)); os.makedirs(d)
    with open(os.path.join(d, "identity"), "w") as f:
        f.write("data-bus-mcp-a3f29c1b")
    assert c.read_identity(sid, root=str(tmp_path)) == "data-bus-mcp-a3f29c1b"
    assert c.read_identity("nope", root=str(tmp_path)) is None


# --- cursor + watcher liveness (pid + start-time) -------------------------

def test_cursor_roundtrip_and_default(tmp_path):
    sid = "s"; os.makedirs(c.state_dir(sid, root=str(tmp_path)))
    assert c.read_cursor(sid, root=str(tmp_path)) == 0      # absent → 0
    c.write_cursor(sid, 42, root=str(tmp_path))
    assert c.read_cursor(sid, root=str(tmp_path)) == 42


def test_watcher_alive_self_true(tmp_path):
    sid = "s"; os.makedirs(c.state_dir(sid, root=str(tmp_path)))
    pid = os.getpid()
    st = c._proc_starttime(pid)
    assert st is not None
    c.write_watcher_pid(sid, pid, st, root=str(tmp_path))
    assert c.watcher_alive(sid, root=str(tmp_path)) is True


def test_watcher_alive_false_on_starttime_mismatch(tmp_path):
    # simulate PID reuse: same pid recorded but with a stale start-time string
    sid = "s"; os.makedirs(c.state_dir(sid, root=str(tmp_path)))
    c.write_watcher_pid(sid, os.getpid(), "Thu Jan  1 00:00:00 2000", root=str(tmp_path))
    assert c.watcher_alive(sid, root=str(tmp_path)) is False


def test_watcher_alive_false_when_absent(tmp_path):
    sid = "s"; os.makedirs(c.state_dir(sid, root=str(tmp_path)))
    assert c.watcher_alive(sid, root=str(tmp_path)) is False


# --- daemon liveness probe + register (live temp daemon on alt port) -------

import threading, time  # noqa: E402


def _boot_daemon(tmp_path, port):
    import bus_server as b
    b.PORT = port; b.DB = str(tmp_path / "e2e.sqlite3"); b._init(b.DB)
    threading.Thread(target=lambda: b.mcp.run(transport="http", host="127.0.0.1", port=port),
                     daemon=True).start()
    time.sleep(2.5)
    return b


def test_daemon_up_false_when_down():
    c.BASE = "http://127.0.0.1:9131"   # nothing listening
    assert c.daemon_up(timeout=1.0) is False


def test_daemon_up_and_register_roundtrip(tmp_path):
    b = _boot_daemon(tmp_path, 9132)
    c.BASE = "http://127.0.0.1:9132"
    assert c.daemon_up(timeout=2.0) is True
    assert c.register("backend-1", repo="backend", current_task="checkout") is True
    sessions = b._list_sessions(b.DB)
    assert any(s["handle"] == "backend-1" and s["current_task"] == "checkout" for s in sessions)


# --- watcher-launch directive (model-owned; R2: carries session_id) -------

def test_watch_command_shape():
    cmd = c.watch_command("s", "h", 7, plugin_root="/p")
    assert cmd == "bash /p/bus_watch.sh s h 7"


def test_launch_directive_embeds_handle_cursor_and_floor(tmp_path):
    sid = "s"; d = c.state_dir(sid, root=str(tmp_path)); os.makedirs(d)
    with open(os.path.join(d, "identity"), "w") as f:
        f.write("data-bus-mcp-a3f29c1b")
    c.write_cursor(sid, 5, root=str(tmp_path))
    msg = c.launch_directive(sid, plugin_root="/p", root=str(tmp_path))
    assert "data-bus-mcp-a3f29c1b" in msg
    assert "bus_watch.sh s data-bus-mcp-a3f29c1b 5" in msg
    assert "background" in msg.lower()
    assert "wait_for_message" in msg            # the documented floor is offered
    assert "untrusted" in msg.lower()           # security note present
