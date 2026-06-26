import json, os

def _load(p):
    with open(os.path.join(os.path.dirname(__file__), p)) as f:
        return json.load(f)

def test_plugin_json_fields():
    m = _load(".claude-plugin/plugin.json")
    assert m["name"] == "contract-bus" and m["description"] and m["author"]["name"]

def test_marketplace_self_source():
    mk = _load(".claude-plugin/marketplace.json")
    assert mk["$schema"].endswith("marketplace.schema.json")
    p = next(p for p in mk["plugins"] if p["name"] == "contract-bus")
    assert p["source"] == "./" and p["description"]

def test_mcp_json_http_connect_only():
    s = _load(".mcp.json")["mcpServers"]["contract-bus"]
    assert s["type"] == "http"
    assert s["url"] == "http://127.0.0.1:9100/mcp"
    assert "command" not in s
