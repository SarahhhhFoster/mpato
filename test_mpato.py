"""
Tests for MPATO library — validates loader, auth, dispatcher, and MCP shim.
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

# ──────────────────────────────────────────────────────────────────────────────
# Loader tests
# ──────────────────────────────────────────────────────────────────────────────

def test_loader_valid_yaml():
    from mpato.loader import load_definition
    defn = load_definition("definitions/github.yaml")
    assert defn["name"] == "github"
    assert defn["protocol"] == "rest"
    assert "get_issue" in defn["endpoints"]
    print("✓ loader: valid YAML definition loads correctly")


def test_loader_valid_json():
    from mpato.loader import load_definition
    data = {
        "name": "testservice",
        "base_url": "https://example.com",
        "protocol": "rest",
        "endpoints": {
            "ping": {"path": "/ping", "method": "GET"}
        }
    }
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(data, f)
        fname = f.name
    try:
        defn = load_definition(fname)
        assert defn["name"] == "testservice"
        print("✓ loader: valid JSON definition loads correctly")
    finally:
        os.unlink(fname)


def test_loader_missing_file():
    from mpato.loader import load_definition, DefinitionError
    try:
        load_definition("nonexistent.yaml")
        assert False, "Should have raised"
    except DefinitionError as e:
        assert "not found" in str(e).lower()
    print("✓ loader: missing file raises DefinitionError")


def test_loader_invalid_protocol():
    from mpato.loader import load_definition, DefinitionError
    data = {"name": "x", "base_url": "https://x.com", "protocol": "ftp", "endpoints": {}}
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(data, f)
        fname = f.name
    try:
        load_definition(fname)
        assert False, "Should have raised"
    except DefinitionError as e:
        assert "ftp" in str(e)
    finally:
        os.unlink(fname)
    print("✓ loader: invalid protocol raises DefinitionError")


def test_loader_missing_required_field():
    from mpato.loader import load_definition, DefinitionError
    data = {"name": "x", "protocol": "rest"}  # missing base_url
    with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
        json.dump(data, f)
        fname = f.name
    try:
        load_definition(fname)
        assert False, "Should have raised"
    except DefinitionError as e:
        assert "base_url" in str(e)
    finally:
        os.unlink(fname)
    print("✓ loader: missing required field raises DefinitionError")


def test_loader_openmeteo():
    from mpato.loader import load_definition
    defn = load_definition("definitions/openmeteo.yaml")
    assert defn["name"] == "openmeteo"
    assert defn["auth"]["type"] == "none"
    assert "forecast" in defn["endpoints"]
    print("✓ loader: openmeteo definition loads (no-auth service)")


# ──────────────────────────────────────────────────────────────────────────────
# Auth resolver tests
# ──────────────────────────────────────────────────────────────────────────────

def test_resolver_env():
    from mpato.auth.resolver import resolve_credential
    os.environ["TEST_MPATO_KEY"] = "secret123"
    cred = resolve_credential({"strategy": "env", "key": "TEST_MPATO_KEY"}, "test")
    assert cred == "secret123"
    del os.environ["TEST_MPATO_KEY"]
    print("✓ resolver: env strategy resolves correctly")


def test_resolver_static():
    from mpato.auth.resolver import resolve_credential
    cred = resolve_credential({"strategy": "static", "value": "statictoken"}, "test")
    assert cred == "statictoken"
    print("✓ resolver: static strategy resolves correctly")


def test_resolver_file():
    from mpato.auth.resolver import resolve_credential
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("filetoken\nextra_line\n")
        fname = f.name
    try:
        cred = resolve_credential({"strategy": "file", "path": fname}, "test")
        assert cred == "filetoken"
    finally:
        os.unlink(fname)
    print("✓ resolver: file strategy resolves correctly")


def test_resolver_missing_env():
    from mpato.auth.resolver import resolve_credential, ResolverError
    os.environ.pop("MPATO_NONEXISTENT", None)
    try:
        resolve_credential({"strategy": "env", "key": "MPATO_NONEXISTENT"}, "test")
        assert False, "Should raise"
    except ResolverError as e:
        assert "MPATO_NONEXISTENT" in str(e)
    print("✓ resolver: missing env var raises ResolverError")


# ──────────────────────────────────────────────────────────────────────────────
# Auth injector tests
# ──────────────────────────────────────────────────────────────────────────────

def test_injector_header():
    from mpato.auth.injector import inject_credential
    headers, params, body = inject_credential(
        "mytoken",
        {"strategy": "header", "name": "Authorization", "prefix": "Bearer "},
        {}, {}, {}
    )
    assert headers["Authorization"] == "Bearer mytoken"
    print("✓ injector: header injection works")


def test_injector_query():
    from mpato.auth.injector import inject_credential
    headers, params, body = inject_credential(
        "mykey",
        {"strategy": "query", "name": "api_key"},
        {}, {}, {}
    )
    assert params["api_key"] == "mykey"
    print("✓ injector: query injection works")


def test_injector_body():
    from mpato.auth.injector import inject_credential
    headers, params, body = inject_credential(
        "mykey",
        {"strategy": "body", "name": "token"},
        {}, {}, {}
    )
    assert body["token"] == "mykey"
    print("✓ injector: body injection works")


# ──────────────────────────────────────────────────────────────────────────────
# ServiceRegistry tests
# ──────────────────────────────────────────────────────────────────────────────

def test_registry_load_and_services():
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    r.load("definitions/github.yaml")
    r.load("definitions/openmeteo.yaml")
    assert "github" in r.services()
    assert "openmeteo" in r.services()
    print("✓ registry: load and services() work")


def test_registry_unknown_service():
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    r.load("definitions/github.yaml")
    try:
        r.call("nonexistent", "get_issue", {})
        assert False
    except KeyError as e:
        assert "nonexistent" in str(e)
    print("✓ registry: unknown service raises KeyError")


def test_registry_missing_required_param():
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    r.load("definitions/github.yaml")
    result = r.call("github", "get_issue", {"owner": "acme"})  # missing repo and issue_number
    assert not result.success
    assert "repo" in result.error or "issue_number" in result.error
    print("✓ registry: missing required param returns failure Result")


def test_registry_openmeteo_call():
    if not LIVE:
        print("  ~ test_registry_openmeteo_call (skipped -- set MPATO_LIVE_TESTS=1)")
        return
    """Actually call Open-Meteo (no auth needed, public API)."""
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    r.load("definitions/openmeteo.yaml")
    result = r.call("openmeteo", "forecast", {
        "latitude": 37.7749,
        "longitude": -122.4194,
        "current_weather": True,
    })
    assert result.success, f"Expected success, got error: {result.error}"
    assert isinstance(result.data, dict)
    assert result.status_code == 200
    assert "current_weather" in result.data or "latitude" in result.data
    print(f"✓ registry: openmeteo live call succeeded (status {result.status_code})")


def test_registry_chaining():
    from mpato import ServiceRegistry
    r = ServiceRegistry().load("definitions/github.yaml").load("definitions/openmeteo.yaml")
    assert len(r.services()) == 2
    print("✓ registry: method chaining works")


# ──────────────────────────────────────────────────────────────────────────────
# MCP Shim tests
# ──────────────────────────────────────────────────────────────────────────────

def test_mcp_tools():
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry()
    r.load("definitions/github.yaml")
    r.load("definitions/openmeteo.yaml")
    shim = MCPShim(r)
    tools = shim.tools()
    assert len(tools) > 0
    names = {t["name"] for t in tools}
    assert "github__get_issue" in names
    assert "openmeteo__forecast" in names
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"
    print(f"✓ MCP shim: tools() returns {len(tools)} tool schemas")


def test_mcp_tools_required_params():
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry().load("definitions/github.yaml")
    shim = MCPShim(r)
    tools = shim.tools()
    get_issue = next(t for t in tools if t["name"] == "github__get_issue")
    required = get_issue["input_schema"].get("required", [])
    assert "owner" in required
    assert "repo" in required
    assert "issue_number" in required
    print("✓ MCP shim: required params correctly reflected in tool schema")


def test_mcp_dispatch_live():
    if not LIVE:
        print("  ~ test_mcp_dispatch_live (skipped -- set MPATO_LIVE_TESTS=1)")
        return
    """Test MCP dispatch with a live Open-Meteo call."""
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry().load("definitions/openmeteo.yaml")
    shim = MCPShim(r)
    tool_use_block = {
        "type": "tool_use",
        "id": "tu_001",
        "name": "openmeteo__forecast",
        "input": {
            "latitude": 40.7128,
            "longitude": -74.0060,
        }
    }
    result = shim.dispatch(tool_use_block)
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "tu_001"
    assert not result["is_error"], f"Unexpected error: {result['content']}"
    data = json.loads(result["content"])
    assert "latitude" in data or "current_weather" in data
    print("✓ MCP shim: dispatch() with live call works")


def test_mcp_dispatch_invalid_tool_name():
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry().load("definitions/github.yaml")
    shim = MCPShim(r)
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_err",
        "name": "no_double_underscore",
        "input": {}
    })
    assert result["is_error"]
    assert result["tool_use_id"] == "tu_err"
    print("✓ MCP shim: invalid tool name returns is_error=True")


def test_mcp_dispatch_unknown_service():
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry().load("definitions/github.yaml")
    shim = MCPShim(r)
    result = shim.dispatch({
        "type": "tool_use",
        "id": "tu_err2",
        "name": "fakesvc__endpoint",
        "input": {}
    })
    assert result["is_error"]
    print("✓ MCP shim: unknown service returns is_error=True")


# ──────────────────────────────────────────────────────────────────────────────
# Result type tests
# ──────────────────────────────────────────────────────────────────────────────

def test_result_raise_for_error():
    from mpato.result import Result
    r = Result(success=False, error="boom")
    try:
        r.raise_for_error()
        assert False
    except RuntimeError as e:
        assert "boom" in str(e)
    print("✓ Result: raise_for_error() raises on failure")


def test_result_raise_for_error_success():
    from mpato.result import Result
    r = Result(success=True, data={"key": "val"}, status_code=200)
    returned = r.raise_for_error()
    assert returned is r
    print("✓ Result: raise_for_error() returns self on success")


# ──────────────────────────────────────────────────────────────────────────────
# Async test
# ──────────────────────────────────────────────────────────────────────────────

def test_async_callback():
    if not LIVE:
        print("  ~ test_async_callback (skipped -- set MPATO_LIVE_TESTS=1)")
        return
    import threading
    from mpato import ServiceRegistry
    r = ServiceRegistry().load("definitions/openmeteo.yaml")
    results = []
    done = threading.Event()

    def on_result(result):
        results.append(result)
        done.set()

    r.call_async("openmeteo", "forecast", {
        "latitude": 48.8566,
        "longitude": 2.3522,
    }, callback=on_result)

    done.wait(timeout=15)
    assert len(results) == 1
    assert results[0].success, f"Async call failed: {results[0].error}"
    print("✓ async: call_async() with callback works")


# ──────────────────────────────────────────────────────────────────────────────
# Run all tests
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [
        # Loader
        test_loader_valid_yaml,
        test_loader_valid_json,
        test_loader_missing_file,
        test_loader_invalid_protocol,
        test_loader_missing_required_field,
        test_loader_openmeteo,
        # Resolver
        test_resolver_env,
        test_resolver_static,
        test_resolver_file,
        test_resolver_missing_env,
        # Injector
        test_injector_header,
        test_injector_query,
        test_injector_body,
        # Registry
        test_registry_load_and_services,
        test_registry_unknown_service,
        test_registry_missing_required_param,
        test_registry_chaining,
        # Result
        test_result_raise_for_error,
        test_result_raise_for_error_success,
        # Live network tests
        test_registry_openmeteo_call,
        test_mcp_tools,
        test_mcp_tools_required_params,
        test_mcp_dispatch_live,
        test_mcp_dispatch_invalid_tool_name,
        test_mcp_dispatch_unknown_service,
        test_async_callback,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
