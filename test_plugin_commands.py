import os, re
HERE = os.path.dirname(__file__)
def _read(p):
    with open(os.path.join(HERE, p)) as f:
        return f.read()
def test_wrappers_exist_and_invoke_cli():
    j = _read("bus_join.sh")
    assert "ensure-daemon" in j and "join-cli" in j
    assert "git rev-parse --show-toplevel" in j and "$CLAUDE_CODE_SESSION_ID" in j
    assert "conclude-cli" in _read("bus_conclude.sh")
def test_join_command_autoexec_and_allowed_tools():
    b = _read("commands/join.md")
    assert "allowed-tools:" in b and "bus_join.sh" in b
    assert "```!" in b and "${CLAUDE_PLUGIN_ROOT}/bus_join.sh" in b
    assert "$ARGUMENTS" in b
def test_conclude_command_autoexec_and_allowed_tools():
    b = _read("commands/conclude.md")
    assert "allowed-tools:" in b and "```!" in b
    assert "${CLAUDE_PLUGIN_ROOT}/bus_conclude.sh" in b
def test_status_command_calls_list_sessions():
    assert "list_sessions" in _read("commands/status.md")
def test_all_commands_have_description():
    for n in ("join.md", "conclude.md", "status.md"):
        assert re.search(r"^---\n(?:.*\n)*?description:", _read(os.path.join("commands", n)), re.M)
