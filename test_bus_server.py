"""Tests for the contract-bus core logic (spec §10 acceptance criteria + edge cases).

These exercise the pure helper functions (`_init`, `_post_message`, `_read_messages`,
`_list_channels`) against a temp SQLite file, so no HTTP server needs to run.
"""
import asyncio

import pytest

import bus_server as bus


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "bus.sqlite3")
    bus._init(path)
    return path


# --- post_message ---------------------------------------------------------

def test_post_message_returns_id_one_for_first_row(db):
    res = bus._post_message(db, "t", "backend", "hello")
    assert res["id"] == 1
    assert res["channel"] == "t"
    assert res["created_at"]  # non-empty timestamp


def test_post_message_ids_are_monotonic(db):
    assert bus._post_message(db, "t", "backend", "a")["id"] == 1
    assert bus._post_message(db, "t", "frontend", "b")["id"] == 2


def test_post_message_rejects_empty_channel(db):
    res = bus._post_message(db, "", "backend", "hello")
    assert "error" in res


def test_post_message_rejects_empty_body(db):
    res = bus._post_message(db, "t", "backend", "")
    assert "error" in res


# --- read_messages --------------------------------------------------------

def test_read_messages_returns_message(db):
    bus._post_message(db, "t", "backend", "hello")
    rows = bus._read_messages(db, "t", since_id=0)
    assert len(rows) == 1
    assert rows[0]["author"] == "backend"
    assert rows[0]["body"] == "hello"


def test_read_messages_cursor_excludes_seen(db):
    bus._post_message(db, "t", "backend", "hello")
    assert bus._read_messages(db, "t", since_id=1) == []


def test_read_messages_returns_only_newer_than_cursor(db):
    bus._post_message(db, "t", "backend", "hello")
    bus._post_message(db, "t", "frontend", "hi back")
    rows = bus._read_messages(db, "t", since_id=1)
    assert len(rows) == 1
    assert rows[0]["id"] == 2
    assert rows[0]["body"] == "hi back"


def test_read_messages_oldest_first(db):
    bus._post_message(db, "t", "a", "first")
    bus._post_message(db, "t", "b", "second")
    rows = bus._read_messages(db, "t", since_id=0)
    assert [r["body"] for r in rows] == ["first", "second"]


def test_read_messages_isolated_by_channel(db):
    bus._post_message(db, "chan-a", "x", "a")
    bus._post_message(db, "chan-b", "y", "b")
    rows = bus._read_messages(db, "chan-a", since_id=0)
    assert len(rows) == 1
    assert rows[0]["body"] == "a"


def test_read_messages_clamps_limit_to_max(db):
    for i in range(250):
        bus._post_message(db, "t", "a", f"m{i}")
    rows = bus._read_messages(db, "t", since_id=0, limit=10_000)
    assert len(rows) == 200  # clamped to MAX_LIMIT


def test_read_messages_honors_limit(db):
    for i in range(5):
        bus._post_message(db, "t", "a", f"m{i}")
    rows = bus._read_messages(db, "t", since_id=0, limit=2)
    assert len(rows) == 2


# --- list_channels --------------------------------------------------------

def test_list_channels_reports_count_and_last_id(db):
    bus._post_message(db, "t", "backend", "a")
    bus._post_message(db, "t", "frontend", "b")
    chans = bus._list_channels(db)
    assert chans == [{"channel": "t", "message_count": 2, "last_id": 2}]


def test_list_channels_empty_when_no_messages(db):
    assert bus._list_channels(db) == []


# --- self-documentation ---------------------------------------------------

def test_usage_guide_explains_purpose_and_workflow():
    g = bus._usage()
    assert isinstance(g, str)
    low = g.lower()
    # purpose + the three tools + the cursor concept must all be discoverable
    for term in ("contract", "post_message", "read_messages", "list_channels",
                 "wait_for_message", "watch_channel", "since_id", "channel"):
        assert term in low, f"usage guide missing {term!r}"


def test_server_instructions_set_for_handshake_discovery():
    # FastMCP sends `instructions` during the initialize handshake, so a connecting
    # session discovers the bus without calling any tool.
    assert bus.mcp.instructions
    assert "post_message" in bus.mcp.instructions


# --- wait_for_message (blocking long-poll) --------------------------------

def test_wait_for_message_returns_empty_on_timeout(db):
    # nothing posted → returns [] once the (short) timeout elapses
    rows = asyncio.run(
        bus._wait_for_message(db, "t", since_id=0, timeout=0.05, poll=0.01)
    )
    assert rows == []


def test_wait_for_message_returns_message_posted_during_wait(db):
    # a message posted WHILE the call is blocked must wake it and be returned
    async def scenario():
        async def insert_later():
            await asyncio.sleep(0.03)
            bus._post_message(db, "t", "backend", "late arrival")

        task = asyncio.create_task(insert_later())
        rows = await bus._wait_for_message(db, "t", since_id=0, timeout=2.0, poll=0.01)
        await task
        return rows

    rows = asyncio.run(scenario())
    assert len(rows) == 1
    assert rows[0]["body"] == "late arrival"


def test_wait_for_message_ignores_messages_at_or_below_since_id(db):
    # an existing message at id=1 must NOT satisfy a waiter watching since_id=1
    bus._post_message(db, "t", "backend", "old")
    rows = asyncio.run(
        bus._wait_for_message(db, "t", since_id=1, timeout=0.05, poll=0.01)
    )
    assert rows == []


# --- watch_channel (directive: build the backgroundable curl) --------------

def test_watch_command_builds_background_curl(db):
    out = bus._watch_command("feature-checkout", 42)
    cmd = out["run_in_background"]
    assert "curl" in cmd
    assert "/wait" in cmd
    assert "channel=feature-checkout" in cmd
    assert "since_id=42" in cmd
    assert str(bus.PORT) in cmd


def test_watch_command_urlencodes_channel(db):
    out = bus._watch_command("feat x/y", 0)
    cmd = out["run_in_background"]
    assert "feat%20x" in cmd        # space encoded
    assert "feat x" not in cmd       # raw space not present


# --- source auto-reload watcher -------------------------------------------

def test_source_changed_false_when_unmodified(tmp_path):
    f = tmp_path / "src.py"
    f.write_text("v1")
    base = bus._source_mtime(str(f))
    assert bus._source_changed(str(f), base) is False


def test_source_changed_true_after_modification(tmp_path):
    import os as _os
    f = tmp_path / "src.py"
    f.write_text("v1")
    base = bus._source_mtime(str(f))
    _os.utime(str(f), (base + 5, base + 5))   # deterministic mtime bump
    assert bus._source_changed(str(f), base) is True


def test_source_changed_false_when_path_missing(tmp_path):
    # a transient missing file (e.g. mid atomic-save) must NOT count as a change → no exit
    assert bus._source_changed(str(tmp_path / "gone.py"), 123.0) is False


# --- persistence ----------------------------------------------------------

def test_messages_persist_across_reconnect(db):
    bus._post_message(db, "t", "backend", "hello")
    bus._post_message(db, "t", "frontend", "hi back")
    # simulate daemon restart: nothing cached, reopen the same file
    rows = bus._read_messages(db, "t", since_id=0)
    assert len(rows) == 2
