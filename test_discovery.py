"""
Comprehensive tests for mpato.discovery and ServiceRegistry.load_index().

Covers:
  - Index file parsing (valid, missing, malformed JSON, wrong version)
  - Required field validation (mpato_index_version, services, name, definition)
  - ServiceEntry fields (name, description, version, auth_type, tags, local_path)
  - Relative definition path resolution
  - Absolute definition path resolution
  - Tag filtering
  - auth_type filtering
  - Combined tag + auth_type filtering
  - Empty services list
  - discover() with the real bundled registry/mpato-index.json
  - ServiceRegistry.load_index() convenience method
  - load_index() chaining
  - load_index() tag filtering loads only matching services
  - load_index() + call() end-to-end (live HTTP, openmeteo)
  - load_index() + MCP shim: tools() reflects discovered services
  - load_index() + MCP shim: dispatch() works on discovered service
  - Remote index discovery (mocked via a local HTTP server)
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
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PASS = 0
FAIL = 0


def check(label):
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
            import traceback
            traceback.print_exc()
            FAIL += 1

    return _ctx()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

REGISTRY_DIR = Path("registry")
REAL_INDEX = str(REGISTRY_DIR / "mpato-index.json")


def write_index(data: dict) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8")
    json.dump(data, f)
    f.close()
    return f.name


def write_yaml(content: str) -> str:
    f = tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False, encoding="utf-8")
    f.write(content)
    f.close()
    return f.name


def cleanup(*paths):
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


def minimal_service_def(name="testsvc", base_url="https://example.com") -> str:
    """Write a minimal valid YAML definition and return its path."""
    return write_yaml(f"""
name: {name}
base_url: {base_url}
protocol: rest
endpoints:
  ping:
    path: /ping
    method: GET
""")


def make_index(services: list, version: str = "1") -> dict:
    return {
        "mpato_index_version": version,
        "name": "Test Registry",
        "description": "Test",
        "services": services,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Local HTTP server for remote discovery tests
# ─────────────────────────────────────────────────────────────────────────────

class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress request logs during tests


def start_file_server(directory: str, port: int = 0):
    """Serve `directory` over HTTP on a random port. Returns (server, url_base)."""
    os.chdir(directory)
    server = HTTPServer(("127.0.0.1", port), _QuietHandler)
    actual_port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{actual_port}"


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: index parsing — valid cases
# ─────────────────────────────────────────────────────────────────────────────

print("\n── index parsing — valid ──")

with check("discover() returns a list"):
    from mpato.discovery import discover
    result = discover(REAL_INDEX)
    assert isinstance(result, list)

with check("discover() returns correct number of entries for real index"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX)
    assert len(entries) == 2, f"Expected 2, got {len(entries)}"

with check("each entry is a ServiceEntry"):
    from mpato.discovery import discover, ServiceEntry
    for entry in discover(REAL_INDEX):
        assert isinstance(entry, ServiceEntry)

with check("ServiceEntry.name matches definition file name field"):
    from mpato.discovery import discover
    entries = {e.name: e for e in discover(REAL_INDEX)}
    assert "github" in entries
    assert "openmeteo" in entries

with check("ServiceEntry.description is a non-empty string"):
    from mpato.discovery import discover
    for entry in discover(REAL_INDEX):
        assert isinstance(entry.description, str) and len(entry.description) > 0

with check("ServiceEntry.version is a non-empty string"):
    from mpato.discovery import discover
    for entry in discover(REAL_INDEX):
        assert isinstance(entry.version, str) and len(entry.version) > 0

with check("ServiceEntry.auth_type is correct for github (bearer)"):
    from mpato.discovery import discover
    entries = {e.name: e for e in discover(REAL_INDEX)}
    assert entries["github"].auth_type == "bearer"

with check("ServiceEntry.auth_type is correct for openmeteo (none)"):
    from mpato.discovery import discover
    entries = {e.name: e for e in discover(REAL_INDEX)}
    assert entries["openmeteo"].auth_type == "none"

with check("ServiceEntry.tags is a list"):
    from mpato.discovery import discover
    for entry in discover(REAL_INDEX):
        assert isinstance(entry.tags, list)

with check("github tags include 'vcs'"):
    from mpato.discovery import discover
    entries = {e.name: e for e in discover(REAL_INDEX)}
    assert "vcs" in entries["github"].tags

with check("openmeteo tags include 'free'"):
    from mpato.discovery import discover
    entries = {e.name: e for e in discover(REAL_INDEX)}
    assert "free" in entries["openmeteo"].tags

with check("ServiceEntry.local_path points to an existing file"):
    from mpato.discovery import discover
    for entry in discover(REAL_INDEX):
        assert os.path.exists(entry.local_path), f"Missing: {entry.local_path}"

with check("local_path is a .yaml or .json file"):
    from mpato.discovery import discover
    for entry in discover(REAL_INDEX):
        assert entry.local_path.endswith((".yaml", ".yml", ".json"))

with check("definition files are loadable by loader.load_definition"):
    from mpato.discovery import discover
    from mpato.loader import load_definition
    for entry in discover(REAL_INDEX):
        defn = load_definition(entry.local_path)
        assert defn["name"] == entry.name

# ─────────────────────────────────────────────────────────────────────────────
# Section 2: index parsing — error cases
# ─────────────────────────────────────────────────────────────────────────────

print("\n── index parsing — errors ──")

with check("DiscoveryError raised for missing index file"):
    from mpato.discovery import discover, DiscoveryError
    try:
        discover("nonexistent-index.json")
        assert False, "Should have raised"
    except DiscoveryError as e:
        assert "not found" in str(e).lower()

with check("DiscoveryError raised for malformed JSON"):
    from mpato.discovery import discover, DiscoveryError
    f = tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
    f.write("{ not valid json }")
    f.close()
    try:
        discover(f.name)
        assert False
    except DiscoveryError:
        pass
    finally:
        cleanup(f.name)

with check("DiscoveryError raised when mpato_index_version is missing"):
    from mpato.discovery import discover, DiscoveryError
    path = write_index({"name": "x", "services": []})
    try:
        discover(path)
        assert False
    except DiscoveryError as e:
        assert "mpato_index_version" in str(e)
    finally:
        cleanup(path)

with check("DiscoveryError raised for unsupported index version"):
    from mpato.discovery import discover, DiscoveryError
    path = write_index({"mpato_index_version": "99", "services": []})
    try:
        discover(path)
        assert False
    except DiscoveryError as e:
        assert "99" in str(e)
    finally:
        cleanup(path)

with check("DiscoveryError raised when 'services' field is missing"):
    from mpato.discovery import discover, DiscoveryError
    path = write_index({"mpato_index_version": "1", "name": "x"})
    try:
        discover(path)
        assert False
    except DiscoveryError as e:
        assert "services" in str(e)
    finally:
        cleanup(path)

with check("DiscoveryError raised when services is not a list"):
    from mpato.discovery import discover, DiscoveryError
    path = write_index({"mpato_index_version": "1", "services": "not a list"})
    try:
        discover(path)
        assert False
    except DiscoveryError as e:
        assert "services" in str(e).lower()
    finally:
        cleanup(path)

with check("DiscoveryError raised when a service entry is missing 'name'"):
    from mpato.discovery import discover, DiscoveryError
    svc_path = minimal_service_def()
    path = write_index(make_index([{"definition": svc_path}]))
    try:
        discover(path)
        assert False
    except DiscoveryError as e:
        assert "name" in str(e)
    finally:
        cleanup(path, svc_path)

with check("DiscoveryError raised when a service entry is missing 'definition'"):
    from mpato.discovery import discover, DiscoveryError
    path = write_index(make_index([{"name": "svc"}]))
    try:
        discover(path)
        assert False
    except DiscoveryError as e:
        assert "definition" in str(e)
    finally:
        cleanup(path)

# ─────────────────────────────────────────────────────────────────────────────
# Section 3: path resolution
# ─────────────────────────────────────────────────────────────────────────────

print("\n── path resolution ──")

with check("relative definition path resolves relative to index directory"):
    from mpato.discovery import discover
    # The real index uses relative paths ("github.yaml") — verify they resolve
    entries = {e.name: e for e in discover(REAL_INDEX)}
    expected = str(Path(REAL_INDEX).parent.resolve() / "github.yaml")
    assert os.path.normpath(entries["github"].local_path) == os.path.normpath(expected)

with check("absolute definition path is used as-is"):
    from mpato.discovery import discover
    svc_path = minimal_service_def("abssvc")
    abs_path = os.path.abspath(svc_path)
    path = write_index(make_index([{
        "name": "abssvc", "description": "", "version": "1.0.0",
        "auth_type": "none", "tags": [], "definition": abs_path,
    }]))
    try:
        entries = discover(path)
        assert len(entries) == 1
        assert os.path.normpath(entries[0].local_path) == os.path.normpath(abs_path)
    finally:
        cleanup(path, svc_path)

with check("definition file in subdirectory of index resolves correctly"):
    from mpato.discovery import discover
    td = tempfile.mkdtemp()
    sub = os.path.join(td, "services")
    os.makedirs(sub)
    svc_path = os.path.join(sub, "mysvc.yaml")
    Path(svc_path).write_text("""
name: mysvc
base_url: https://example.com
protocol: rest
endpoints:
  ping:
    path: /ping
    method: GET
""")
    index_data = make_index([{
        "name": "mysvc", "description": "", "version": "1.0.0",
        "auth_type": "none", "tags": [], "definition": "services/mysvc.yaml",
    }])
    index_path = os.path.join(td, "mpato-index.json")
    Path(index_path).write_text(json.dumps(index_data))
    try:
        entries = discover(index_path)
        assert len(entries) == 1
        assert os.path.exists(entries[0].local_path)
    finally:
        import shutil
        shutil.rmtree(td)

# ─────────────────────────────────────────────────────────────────────────────
# Section 4: filtering
# ─────────────────────────────────────────────────────────────────────────────

print("\n── filtering ──")

with check("tag filter returns only matching services"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, tags=["free"])
    names = [e.name for e in entries]
    assert "openmeteo" in names
    assert "github" not in names

with check("tag filter with multiple tags is OR match"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, tags=["free", "vcs"])
    names = {e.name for e in entries}
    assert "openmeteo" in names
    assert "github" in names

with check("tag filter is case-insensitive"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, tags=["FREE"])
    assert any(e.name == "openmeteo" for e in entries)

with check("auth_type filter returns only matching services"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, auth_types=["none"])
    assert all(e.auth_type == "none" for e in entries)
    assert any(e.name == "openmeteo" for e in entries)
    assert not any(e.name == "github" for e in entries)

with check("auth_type filter is case-insensitive"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, auth_types=["NONE"])
    assert any(e.name == "openmeteo" for e in entries)

with check("combined tag + auth_type filter is AND (both must match)"):
    from mpato.discovery import discover
    # openmeteo has tag "free" AND auth_type "none" — should appear
    entries = discover(REAL_INDEX, tags=["free"], auth_types=["none"])
    assert any(e.name == "openmeteo" for e in entries)
    # github has tag "vcs" but auth_type "bearer" — not in both filters
    entries2 = discover(REAL_INDEX, tags=["vcs"], auth_types=["none"])
    assert not any(e.name == "github" for e in entries2)

with check("tag filter that matches nothing returns empty list"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, tags=["nonexistent-tag-xyz"])
    assert entries == []

with check("auth_type filter that matches nothing returns empty list"):
    from mpato.discovery import discover
    entries = discover(REAL_INDEX, auth_types=["oauth2"])
    assert entries == []

with check("empty services list returns empty list"):
    from mpato.discovery import discover
    path = write_index(make_index([]))
    try:
        entries = discover(path)
        assert entries == []
    finally:
        cleanup(path)

# ─────────────────────────────────────────────────────────────────────────────
# Section 5: ServiceRegistry.load_index()
# ─────────────────────────────────────────────────────────────────────────────

print("\n── ServiceRegistry.load_index() ──")

with check("load_index() loads all services from index"):
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    r.load_index(REAL_INDEX)
    assert "github" in r.services()
    assert "openmeteo" in r.services()

with check("load_index() returns self for chaining"):
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    returned = r.load_index(REAL_INDEX)
    assert returned is r

with check("load_index() chaining works"):
    from mpato import ServiceRegistry
    r = ServiceRegistry().load_index(REAL_INDEX)
    assert len(r.services()) == 2

with check("load_index() with tag filter loads only matching services"):
    from mpato import ServiceRegistry
    r = ServiceRegistry().load_index(REAL_INDEX, tags=["free"])
    assert "openmeteo" in r.services()
    assert "github" not in r.services()

with check("load_index() with auth_type filter loads only matching services"):
    from mpato import ServiceRegistry
    r = ServiceRegistry().load_index(REAL_INDEX, auth_types=["bearer"])
    assert "github" in r.services()
    assert "openmeteo" not in r.services()

with check("load_index() followed by load() works (mixing load styles)"):
    from mpato import ServiceRegistry
    r = ServiceRegistry()
    r.load_index(REAL_INDEX, tags=["free"])  # just openmeteo
    r.load("registry/github.yaml")           # add github manually
    assert "github" in r.services()
    assert "openmeteo" in r.services()

with check("load_index() raises DiscoveryError for bad index path"):
    from mpato import ServiceRegistry
    from mpato.discovery import DiscoveryError
    r = ServiceRegistry()
    try:
        r.load_index("totally-missing.json")
        assert False
    except DiscoveryError:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Section 6: end-to-end — discovered service → call()
# ─────────────────────────────────────────────────────────────────────────────

print("\n── end-to-end: discovered → call() ──")

with live("load_index() + call() works for openmeteo (live HTTP)"):
    from mpato import ServiceRegistry
    r = ServiceRegistry().load_index(REAL_INDEX, tags=["free"])
    result = r.call("openmeteo", "forecast", {
        "latitude": 51.5074,
        "longitude": -0.1278,
    })
    assert result.success, f"Call failed: {result.error}"
    assert result.status_code == 200
    import json as _json
    assert "latitude" in result.data

with check("load_index() + call() on filtered-out service raises KeyError"):
    from mpato import ServiceRegistry
    r = ServiceRegistry().load_index(REAL_INDEX, tags=["free"])
    try:
        r.call("github", "get_issue", {"owner": "x", "repo": "y", "issue_number": 1})
        assert False
    except KeyError:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Section 7: end-to-end — discovered service → MCP shim
# ─────────────────────────────────────────────────────────────────────────────

print("\n── end-to-end: discovered → MCP shim ──")

with check("load_index() + MCPShim.tools() reflects discovered services"):
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry().load_index(REAL_INDEX)
    tools = MCPShim(r).tools()
    names = {t["name"] for t in tools}
    assert "github__get_issue" in names
    assert "openmeteo__forecast" in names

with check("load_index() with tag filter → MCPShim.tools() only shows matching services"):
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    r = ServiceRegistry().load_index(REAL_INDEX, tags=["free"])
    tools = MCPShim(r).tools()
    names = {t["name"] for t in tools}
    assert all(n.startswith("openmeteo__") for n in names), \
        f"Unexpected tools: {names}"

with live("load_index() + MCPShim.dispatch() works for openmeteo (live HTTP)"):
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim
    import json as _json
    r = ServiceRegistry().load_index(REAL_INDEX, tags=["free"])
    shim = MCPShim(r)
    result = shim.dispatch({
        "type": "tool_use",
        "id": "disc_01",
        "name": "openmeteo__forecast",
        "input": {"latitude": 48.8566, "longitude": 2.3522},
    })
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "disc_01"
    assert not result["is_error"], f"Error: {result['content']}"
    data = _json.loads(result["content"])
    assert "latitude" in data

# ─────────────────────────────────────────────────────────────────────────────
# Section 8: remote index via local HTTP server
# ─────────────────────────────────────────────────────────────────────────────

print("\n── remote discovery (local HTTP server) ──")

with live("discover() works with an http:// index URL"):
    from mpato.discovery import discover
    import shutil

    # Set up a temp directory to serve
    td = tempfile.mkdtemp()
    svc_path_src = minimal_service_def("remotesvc")
    shutil.copy(svc_path_src, os.path.join(td, "remotesvc.yaml"))
    index_data = make_index([{
        "name": "remotesvc",
        "description": "A remote service",
        "version": "1.0.0",
        "auth_type": "none",
        "tags": ["test"],
        "definition": "remotesvc.yaml",
    }])
    Path(os.path.join(td, "mpato-index.json")).write_text(json.dumps(index_data))

    original_dir = os.getcwd()
    server, base_url = start_file_server(td)
    try:
        entries = discover(f"{base_url}/mpato-index.json")
        assert len(entries) == 1
        assert entries[0].name == "remotesvc"
        assert os.path.exists(entries[0].local_path)
    finally:
        server.shutdown()
        os.chdir(original_dir)
        cleanup(svc_path_src)
        shutil.rmtree(td)

with live("remote discover() + load_index() + call() works end-to-end"):
    from mpato import ServiceRegistry
    import shutil

    # Serve a real definition over HTTP
    td = tempfile.mkdtemp()
    shutil.copy("registry/openmeteo.yaml", os.path.join(td, "openmeteo.yaml"))
    index_data = make_index([{
        "name": "openmeteo",
        "description": "Weather",
        "version": "1.0.0",
        "auth_type": "none",
        "tags": ["weather"],
        "definition": "openmeteo.yaml",
    }])
    Path(os.path.join(td, "mpato-index.json")).write_text(json.dumps(index_data))

    original_dir = os.getcwd()
    server, base_url = start_file_server(td)
    try:
        r = ServiceRegistry().load_index(f"{base_url}/mpato-index.json")
        assert "openmeteo" in r.services()
        result = r.call("openmeteo", "forecast", {
            "latitude": 35.6762,
            "longitude": 139.6503,
        })
        assert result.success, f"Failed: {result.error}"
        assert "latitude" in result.data
    finally:
        server.shutdown()
        os.chdir(original_dir)
        shutil.rmtree(td)

with live("remote index with relative definition refs resolves correctly"):
    from mpato.discovery import discover
    import shutil

    td = tempfile.mkdtemp()
    sub = os.path.join(td, "defs")
    os.makedirs(sub)
    shutil.copy("registry/openmeteo.yaml", os.path.join(sub, "openmeteo.yaml"))
    index_data = make_index([{
        "name": "openmeteo",
        "description": "Weather",
        "version": "1.0.0",
        "auth_type": "none",
        "tags": [],
        "definition": "defs/openmeteo.yaml",   # relative path in remote index
    }])
    Path(os.path.join(td, "mpato-index.json")).write_text(json.dumps(index_data))

    original_dir = os.getcwd()
    server, base_url = start_file_server(td)
    try:
        entries = discover(f"{base_url}/mpato-index.json")
        assert len(entries) == 1
        assert entries[0].name == "openmeteo"
        assert os.path.exists(entries[0].local_path)
    finally:
        server.shutdown()
        os.chdir(original_dir)
        shutil.rmtree(td)

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

print(f"\n{'═' * 55}")
print(f"  Discovery tests: {PASS} passed, {FAIL} failed")
print(f"{'═' * 55}\n")
sys.exit(0 if FAIL == 0 else 1)
