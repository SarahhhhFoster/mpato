"""
Comprehensive tests for mpato.shims.mcp.MCPShim

Covers:
  - tools() schema structure and completeness
  - tools() type mapping (all 6 MPATO types → JSON Schema)
  - tools() enum, default, description propagation
  - tools() required vs optional params
  - tools() fallback description generation (no description field)
  - tools() empty registry
  - tools() endpoint with no params
  - tools() tool name format: <service>__<endpoint>
  - tools() name uniqueness across multi-service registries
  - dispatch() success path — real HTTP call (openmeteo, no auth)
  - dispatch() content is valid JSON string
  - dispatch() tool_use_id passthrough
  - dispatch() missing type field (still works)
  - dispatch() missing id field (fallback to "unknown")
  - dispatch() missing input field (defaults to {})
  - dispatch() invalid tool name (no __)
  - dispatch() unknown service
  - dispatch() unknown endpoint
  - dispatch() failed underlying call (missing required param)
  - dispatch() endpoint name containing __ (split on first only)
  - dispatch() is_error=False on success, True on error
  - dispatch() error content is JSON with "error" key
  - Round-trip: tools() schema → valid dispatch() call
"""

import json
import os

LIVE = os.environ.get("MPATO_LIVE_TESTS", "").lower() in ("1", "true", "yes")

def live(label):
    """Wrapper that skips live network tests unless MPATO_LIVE_TESTS=1."""
    import contextlib
    @contextlib.contextmanager
    def _ctx():
        global PASS, FAIL
        if not LIVE:
            print(f"  ~ {label} (skipped — set MPATO_LIVE_TESTS=1 to run)")
            yield  # yield so the with-block body runs but we catch everything
            return
        try:
            yield
            print(f"  ✓ {label}")
            PASS += 1
        except Exception as e:
            print(f"  ✗ {label}")
            print(f"      {type(e).__name__}: {e}")
            FAIL += 1
    # When not LIVE, wrap in a suppressor so assertions/errors are swallowed
    if not LIVE:
        @contextlib.contextmanager
        def _skip():
            try:
                yield
            except Exception:
                pass
            print(f"  ~ {label} (skipped — set MPATO_LIVE_TESTS=1 to run)")
        return _skip()
    return _ctx()

import sys
import tempfile

PASS = 0
FAIL = 0


def check(label):
    """Context manager / decorator that catches assertion errors and reports."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        global PASS, FAIL
        try:
            yield
            print(f"  ✓ {label}")
            PASS += 1
        except Exception as e:
            print(f"  ✗ {label}")
            print(f"      {type(e).__name__}: {e}")
            FAIL += 1

    return _ctx()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_registry(*definition_paths):
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    for p in definition_paths:
        r.load(p)
    return r


def make_shim(*definition_paths):
    from mpato.shims.mcp import MCPShim
    return MCPShim(make_registry(*definition_paths))


def write_yaml(content: str) -> str:
    """Write a temp YAML definition file, return path."""
    f = tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False)
    f.write(content)
    f.close()
    return f.name


def cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: tools() — schema structure
# ─────────────────────────────────────────────────────────────────────────────

print("\n── tools() schema structure ──")

with check("tools() returns a list"):
    shim = make_shim("definitions/github.yaml")
    assert isinstance(shim.tools(), list)

with check("tools() returns non-empty list for loaded service"):
    tools = make_shim("definitions/github.yaml").tools()
    assert len(tools) > 0

with check("every tool has 'name', 'description', 'input_schema' keys"):
    for tool in make_shim("definitions/github.yaml").tools():
        assert "name" in tool, f"missing 'name' in {tool}"
        assert "description" in tool, f"missing 'description' in {tool}"
        assert "input_schema" in tool, f"missing 'input_schema' in {tool}"

with check("input_schema.type is always 'object'"):
    for tool in make_shim("definitions/github.yaml").tools():
        assert tool["input_schema"]["type"] == "object", tool["name"]

with check("input_schema has 'properties' dict"):
    for tool in make_shim("definitions/github.yaml").tools():
        assert isinstance(tool["input_schema"].get("properties"), dict), tool["name"]

with check("tool names are strings"):
    for tool in make_shim("definitions/github.yaml").tools():
        assert isinstance(tool["name"], str)

with check("descriptions are non-empty strings"):
    for tool in make_shim("definitions/github.yaml").tools():
        assert isinstance(tool["description"], str) and len(tool["description"]) > 0

# ─────────────────────────────────────────────────────────────────────────────
# Section 2: tools() — naming
# ─────────────────────────────────────────────────────────────────────────────

print("\n── tools() naming ──")

with check("tool name format is <service>__<endpoint>"):
    for tool in make_shim("definitions/github.yaml").tools():
        assert "__" in tool["name"], f"No __ in tool name: {tool['name']}"
        service, endpoint = tool["name"].split("__", 1)
        assert service == "github"
        assert len(endpoint) > 0

with check("known github endpoints all appear"):
    names = {t["name"] for t in make_shim("definitions/github.yaml").tools()}
    for ep in ["get_issue", "list_issues", "get_repo", "create_issue", "search_repos"]:
        assert f"github__{ep}" in names, f"Missing github__{ep}"

with check("openmeteo endpoints appear"):
    names = {t["name"] for t in make_shim("definitions/openmeteo.yaml").tools()}
    assert "openmeteo__forecast" in names
    assert "openmeteo__air_quality" in names

with check("multi-service registry: all tools from all services appear"):
    tools = make_shim("definitions/github.yaml", "definitions/openmeteo.yaml").tools()
    names = {t["name"] for t in tools}
    assert "github__get_issue" in names
    assert "openmeteo__forecast" in names

with check("no duplicate tool names across multi-service registry"):
    tools = make_shim("definitions/github.yaml", "definitions/openmeteo.yaml").tools()
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)), f"Duplicates: {[n for n in names if names.count(n) > 1]}"

with check("total tool count matches sum of endpoints across services"):
    tools = make_shim("definitions/github.yaml", "definitions/openmeteo.yaml").tools()
    # github has 5, openmeteo has 2
    assert len(tools) == 7, f"Expected 7, got {len(tools)}"

# ─────────────────────────────────────────────────────────────────────────────
# Section 3: tools() — parameter schema fidelity
# ─────────────────────────────────────────────────────────────────────────────

print("\n── tools() parameter schema fidelity ──")

with check("required params appear in input_schema.required"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__get_issue")
    required = t["input_schema"].get("required", [])
    assert "owner" in required
    assert "repo" in required
    assert "issue_number" in required

with check("optional params do NOT appear in required list"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__list_issues")
    required = t["input_schema"].get("required", [])
    assert "state" not in required
    assert "per_page" not in required

with check("endpoint with no required params has no 'required' key or empty list"):
    # openmeteo forecast has required params, but let's check via a custom definition
    path = write_yaml("""
name: svc
base_url: https://example.com
protocol: rest
endpoints:
  ping:
    path: /ping
    method: GET
    params:
      verbose:
        type: boolean
        required: false
""")
    try:
        tools = make_shim(path).tools()
        t = tools[0]
        required = t["input_schema"].get("required", [])
        assert required == [] or "required" not in t["input_schema"]
    finally:
        cleanup(path)

with check("integer param type maps to JSON Schema 'integer'"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__get_issue")
    assert t["input_schema"]["properties"]["issue_number"]["type"] == "integer"

with check("number param type maps to JSON Schema 'number'"):
    tools = make_shim("definitions/openmeteo.yaml").tools()
    t = next(t for t in tools if t["name"] == "openmeteo__forecast")
    assert t["input_schema"]["properties"]["latitude"]["type"] == "number"
    assert t["input_schema"]["properties"]["longitude"]["type"] == "number"

with check("boolean param type maps to JSON Schema 'boolean'"):
    tools = make_shim("definitions/openmeteo.yaml").tools()
    t = next(t for t in tools if t["name"] == "openmeteo__forecast")
    assert t["input_schema"]["properties"]["current_weather"]["type"] == "boolean"

with check("string param type maps to JSON Schema 'string'"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__get_issue")
    assert t["input_schema"]["properties"]["owner"]["type"] == "string"

with check("all 6 MPATO types are tested (array and object via custom def)"):
    path = write_yaml("""
name: typesvc
base_url: https://example.com
protocol: rest
endpoints:
  test:
    path: /test
    method: POST
    params:
      arr_field:
        type: array
        required: false
      obj_field:
        type: object
        required: false
""")
    try:
        tools = make_shim(path).tools()
        t = tools[0]
        props = t["input_schema"]["properties"]
        assert props["arr_field"]["type"] == "array"
        assert props["obj_field"]["type"] == "object"
    finally:
        cleanup(path)

with check("enum values are preserved in property schema"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__list_issues")
    state_prop = t["input_schema"]["properties"]["state"]
    assert "enum" in state_prop
    assert set(state_prop["enum"]) == {"open", "closed", "all"}

with check("default values are preserved in property schema"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__list_issues")
    assert t["input_schema"]["properties"]["state"]["default"] == "open"
    assert t["input_schema"]["properties"]["per_page"]["default"] == 30

with check("param description is preserved"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__get_issue")
    assert "description" in t["input_schema"]["properties"]["owner"]
    assert len(t["input_schema"]["properties"]["owner"]["description"]) > 0

# ─────────────────────────────────────────────────────────────────────────────
# Section 4: tools() — edge cases
# ─────────────────────────────────────────────────────────────────────────────

print("\n── tools() edge cases ──")

with check("empty registry returns empty list"):
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    shim = MCPShim(ServiceRegistry())
    assert shim.tools() == []

with check("endpoint with zero params produces empty properties dict"):
    path = write_yaml("""
name: nosvc
base_url: https://example.com
protocol: rest
endpoints:
  status:
    path: /status
    method: GET
""")
    try:
        tools = make_shim(path).tools()
        assert tools[0]["input_schema"]["properties"] == {}
        assert "required" not in tools[0]["input_schema"] or tools[0]["input_schema"].get("required") == []
    finally:
        cleanup(path)

with check("endpoint description field is used when present"):
    tools = make_shim("definitions/github.yaml").tools()
    t = next(t for t in tools if t["name"] == "github__get_issue")
    assert t["description"] == "Get a single issue from a GitHub repository"

with check("fallback description generated when no description field"):
    path = write_yaml("""
name: nodesc
base_url: https://example.com
protocol: rest
endpoints:
  ping:
    path: /ping
    method: GET
""")
    try:
        tools = make_shim(path).tools()
        assert len(tools[0]["description"]) > 0
    finally:
        cleanup(path)

with check("WSS service endpoints appear in tools()"):
    path = write_yaml("""
name: wsssvc
base_url: wss://stream.example.com
protocol: wss
endpoints:
  subscribe:
    path: /feed
    description: Subscribe to the live feed
    params:
      channel:
        type: string
        required: true
""")
    try:
        tools = make_shim(path).tools()
        assert any(t["name"] == "wsssvc__subscribe" for t in tools)
    finally:
        cleanup(path)

# ─────────────────────────────────────────────────────────────────────────────
# Section 5: dispatch() — success path
# ─────────────────────────────────────────────────────────────────────────────

print("\n── dispatch() success path ──")

with live("dispatch() returns dict with 'type', 'tool_use_id', 'content', 'is_error'"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_01",
        "name": "openmeteo__forecast",
        "input": {"latitude": 51.5074, "longitude": -0.1278},
    })
    assert "type" in result
    assert "tool_use_id" in result
    assert "content" in result
    assert "is_error" in result

with live("dispatch() type field is 'tool_result'"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_02",
        "name": "openmeteo__forecast",
        "input": {"latitude": 35.6762, "longitude": 139.6503},
    })
    assert result["type"] == "tool_result"

with live("dispatch() tool_use_id matches input id"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "my-unique-id-xyz",
        "name": "openmeteo__forecast",
        "input": {"latitude": 48.8566, "longitude": 2.3522},
    })
    assert result["tool_use_id"] == "my-unique-id-xyz"

with live("dispatch() is_error is False on success"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_03",
        "name": "openmeteo__forecast",
        "input": {"latitude": -33.8688, "longitude": 151.2093},
    })
    assert result["is_error"] is False

with live("dispatch() content is a valid JSON string on success"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_04",
        "name": "openmeteo__forecast",
        "input": {"latitude": 40.7128, "longitude": -74.0060},
    })
    assert isinstance(result["content"], str)
    parsed = json.loads(result["content"])  # must not raise
    assert isinstance(parsed, dict)

with live("dispatch() content contains expected API response fields"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_05",
        "name": "openmeteo__forecast",
        "input": {"latitude": 37.7749, "longitude": -122.4194},
    })
    data = json.loads(result["content"])
    assert "latitude" in data, f"Missing 'latitude' in: {list(data.keys())}"
    assert "longitude" in data

# ─────────────────────────────────────────────────────────────────────────────
# Section 6: dispatch() — input handling
# ─────────────────────────────────────────────────────────────────────────────

print("\n── dispatch() input handling ──")

with live("dispatch() works without 'type' key in block"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        # no "type" key
        "id": "tu_notype",
        "name": "openmeteo__forecast",
        "input": {"latitude": 1.0, "longitude": 1.0},
    })
    assert result["tool_use_id"] == "tu_notype"
    assert result["type"] == "tool_result"

with live("dispatch() missing 'id' falls back to 'unknown'"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        # no "id"
        "name": "openmeteo__forecast",
        "input": {"latitude": 1.0, "longitude": 1.0},
    })
    assert result["tool_use_id"] == "unknown"

with live("dispatch() missing 'input' defaults to empty dict (no crash)"):
    shim = make_shim("definitions/openmeteo.yaml")
    # openmeteo forecast has required params so this will fail, but should not raise
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_noinput",
        "name": "openmeteo__forecast",
        # no "input"
    })
    # Should return a tool_result (possibly is_error=True due to missing params, not a crash)
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tu_noinput"

# ─────────────────────────────────────────────────────────────────────────────
# Section 7: dispatch() — error cases
# ─────────────────────────────────────────────────────────────────────────────

print("\n── dispatch() error cases ──")

with check("dispatch() is_error=True when tool name has no __"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e1", "name": "nodunder", "input": {}})
    assert result["is_error"] is True

with check("dispatch() tool_use_id preserved on invalid tool name error"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e2", "name": "nodunder", "input": {}})
    assert result["tool_use_id"] == "e2"

with check("dispatch() is_error=True for unknown service"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e3", "name": "fakesvc__ep", "input": {}})
    assert result["is_error"] is True

with check("dispatch() is_error=True for unknown endpoint"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e4", "name": "github__no_such_endpoint", "input": {}})
    assert result["is_error"] is True

with check("dispatch() is_error=True when required params are missing"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "e5",
        "name": "github__get_issue",
        "input": {},  # missing owner, repo, issue_number
    })
    assert result["is_error"] is True

with check("dispatch() error content is a valid JSON string with 'error' key"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e6", "name": "nodunder", "input": {}})
    assert isinstance(result["content"], str)
    parsed = json.loads(result["content"])
    assert "error" in parsed

with check("dispatch() error message is non-empty string"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e7", "name": "fakesvc__ep", "input": {}})
    parsed = json.loads(result["content"])
    assert isinstance(parsed["error"], str) and len(parsed["error"]) > 0

with check("dispatch() type is still 'tool_result' on error"):
    shim = make_shim("definitions/github.yaml")
    result = shim.dispatch({"type": "tool_use", "id": "e8", "name": "nodunder", "input": {}})
    assert result["type"] == "tool_result"

# ─────────────────────────────────────────────────────────────────────────────
# Section 8: dispatch() — edge cases
# ─────────────────────────────────────────────────────────────────────────────

print("\n── dispatch() edge cases ──")

with live("endpoint name containing __ is handled (split on first __ only)"):
    # Define an endpoint whose name itself contains __
    path = write_yaml("""
name: svc
base_url: https://api.open-meteo.com
protocol: rest
endpoints:
  v1__forecast:
    path: /v1/forecast
    method: GET
    params:
      latitude:
        type: number
        required: true
      longitude:
        type: number
        required: true
""")
    try:
        shim = make_shim(path)
        tools = shim.tools()
        assert any(t["name"] == "svc__v1__forecast" for t in tools)
        # dispatch should route correctly despite __ in endpoint name
        result = shim.dispatch({
            "type": "tool_use",
            "id": "edge1",
            "name": "svc__v1__forecast",
            "input": {"latitude": 52.52, "longitude": 13.41},
        })
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "edge1"
    finally:
        cleanup(path)

with live("dispatch() with extra unknown keys in block does not crash"):
    shim = make_shim("definitions/openmeteo.yaml")
    result = shim.dispatch({
        "type": "tool_use",
        "id": "edge2",
        "name": "openmeteo__forecast",
        "input": {"latitude": 0.0, "longitude": 0.0},
        "extra_key": "ignored",
        "another": 42,
    })
    assert result["type"] == "tool_result"

# ─────────────────────────────────────────────────────────────────────────────
# Section 9: Round-trip — tools() → dispatch()
# ─────────────────────────────────────────────────────────────────────────────

print("\n── round-trip: tools() schema → dispatch() ──")

with live("every tool in tools() can be dispatched without crashing"):
    shim = make_shim("definitions/openmeteo.yaml")
    for tool in shim.tools():
        # Build minimal valid input from required params
        required = tool["input_schema"].get("required", [])
        props = tool["input_schema"]["properties"]
        input_data = {}
        for p in required:
            t = props[p]["type"]
            input_data[p] = {"number": 0.0, "integer": 0, "string": "x", "boolean": False}.get(t, "x")
        result = shim.dispatch({
            "type": "tool_use",
            "id": f"rt_{tool['name']}",
            "name": tool["name"],
            "input": input_data,
        })
        assert result["type"] == "tool_result", f"Bad type for {tool['name']}"

with live("schema required fields match what registry enforces"):
    # tools() says owner/repo/issue_number are required for get_issue
    # dispatching without them should produce is_error=True
    shim = make_shim("definitions/github.yaml")
    tool = next(t for t in shim.tools() if t["name"] == "github__get_issue")
    required = tool["input_schema"]["required"]
    # Dispatch with each required param missing one at a time
    full_input = {"owner": "acme", "repo": "widget", "issue_number": 1}
    for missing in required:
        partial = {k: v for k, v in full_input.items() if k != missing}
        result = shim.dispatch({
            "type": "tool_use",
            "id": f"rt_missing_{missing}",
            "name": "github__get_issue",
            "input": partial,
        })
        assert result["is_error"] is True, \
            f"Expected is_error=True when '{missing}' is missing, got False"

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{'═' * 55}")
print(f"  MCP shim tests: {PASS} passed, {FAIL} failed")
print(f"{'═' * 55}\n")
sys.exit(0 if FAIL == 0 else 1)
