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
