import json, os

def _hooks():
    with open(os.path.join(os.path.dirname(__file__), "hooks", "hooks.json")) as f:
        return json.load(f)["hooks"]

def test_events_and_no_post_tool_use():
    h = _hooks()
    assert set(h) == {"SessionStart", "Stop", "SubagentStop", "SessionEnd"}

def test_commands_gate_then_cli_with_plugin_root():
    for ev, groups in _hooks().items():
        for g in groups:
            for hk in g["hooks"]:
                cmd = hk["command"]
                assert "bus_gate.sh" in cmd and "bus_cli.py" in cmd
                assert "${CLAUDE_PLUGIN_ROOT}" in cmd and hk.get("timeout", 99) <= 10

def test_event_args_and_matcher():
    h = _hooks()
    assert h["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    assert h["SessionStart"][0]["hooks"][0]["command"].rstrip().endswith("session-start")
    assert h["Stop"][0]["hooks"][0]["command"].rstrip().endswith("stop")
    assert h["SessionEnd"][0]["hooks"][0]["command"].rstrip().endswith("session-end")
