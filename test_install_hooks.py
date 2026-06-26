"""The settings.json hook snippet is valid and wires exactly the intended events."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def test_snippet_is_valid_json_and_wires_events():
    with open(os.path.join(HERE, "hooks.settings.snippet.json")) as f:
        snip = json.load(f)
    hooks = snip["hooks"]
    for ev in ("SessionStart", "Stop", "SubagentStop", "SessionEnd"):
        assert ev in hooks, f"missing {ev}"
    # every command pipes the gate into bus_cli (gate-first discipline)
    cmds = [h["command"] for ev in hooks.values() for g in ev for h in g["hooks"]]
    assert all("bus_gate.sh" in cmd and "bus_cli.py" in cmd for cmd in cmds)
    # PostToolUse is intentionally NOT wired (cursor owned by watcher; avoids per-call cost)
    assert "PostToolUse" not in hooks


def test_three_skills_present_with_frontmatter():
    for name in ("join-contract-bus", "orchestrating-contract-bus-sessions",
                 "conclude-bus-session"):
        p = os.path.join(HERE, "skills", name, "SKILL.md")
        assert os.path.exists(p), f"missing skill {name}"
        head = open(p).read(600)
        assert f"name: {name}" in head and "description:" in head
