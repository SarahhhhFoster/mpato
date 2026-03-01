# MPATO — Multi-Protocol Agent Tool Operator

MPATO is designed to allow an AI agent to use tools, without demanding that the owner of said tool develop their own MCP server. The genesis of this is a product of this LinkedIn post, with some also-included comments, that I made:

> Hot take: MCP is stupid and does nothing. Service discovery, streaming, i/o redirection… these are solved problems and implementing it all again opens one more, unnecessary, attack surface. Most companies choke trying to own and maintain one API attack surface. Asking them to open another one, for every service you want an LLM to talk to, is bad engineering. Refusing to conform to existing standards limits your tool access, all while materially enshittifying the API ecosystem. Stop it. Maybe you need a new client? But the world does not need a new server.

> APIs are implemented to act in the way humans need to interact. Services that require streaming have streaming APIs. Services that require auth take auth in a way that mirrors how humans go through the interaction flow. An agent is designed to act the way a human acts. The end result is that API endpoints should work perfectly well with an agent as long as the agent is provided a semantically-appropriate description of the existing API endpoint. I’m not arguing against having shims that make APIs semantic for agents; I’m arguing that those shims should live at the client side and, preferably, not require dedicated services or endpoints.
>
> Further, I would want most of the business logic to describe interaction patterns; general REST, WSS, OAuth, OpenAPI, etc.; with one module of business logic to handle it. Then a tools interaction service includes the modules, which in turn consume static files describing the respective individual services. All of that can, and I’d argue should, be client-side to avoid server-side accommodation of nonhuman interaction.

## Intent

Build a client-side Python library that allows an AI agent (or any caller) to invoke external APIs — REST and WebSocket — using static declarative definition files (YAML or JSON), with no requirement that the service provider implement any agent-specific protocol. The library should be importable as a module and callable programmatically.

The guiding principle: every capability MCP provides (service discovery, auth, streaming, tool invocation) already exists in the broader ecosystem. MPATO composes those existing capabilities rather than reimplementing them.

---

## Core Design Goals

- **Zero server-side requirements.** Service definitions are static files that can be version-controlled, shared, and served from a CDN. No running process is needed on the service side beyond the API itself.
- **Any API is fair game.** If a service exposes an HTTP or WebSocket API, it can be wrapped with a definition file. The service provider does not need to be involved.
- **Community-extensible.** Anyone can write a definition file for any third-party service and publish it. Consuming a new service is a matter of dropping in a definition file, not standing up infrastructure.
- **Graceful degradation.** The library should handle missing capabilities cleanly — a service definition that omits auth, for example, should still work for services that don't require it.
- **Sync and async first-class.** Both calling patterns should be fully supported without one being an afterthought.

---

## Service Definition Format

Each service is described in a YAML or JSON file. The library loads these at runtime.

A definition file describes:

- **Service metadata**: name, base URL, protocol (`rest` or `wss`)
- **Auth configuration**: type (none, API key, Bearer, OAuth2), where credentials are injected (header, query param, body), and how to resolve the credential value (env var, file, static)
- **Endpoints / channels**: each callable unit with its path, method (for REST), parameter schema, and response handling hints
- **Timeout and retry policy**: per-service or per-endpoint overrides
- **Streaming behavior** (WSS only): end-of-message delimiter, idle timeout in ms after which a buffered response is considered complete

The schema for definition files should be documented and validated on load. Invalid definitions should fail loudly at load time, not silently at call time.

---

## Module Structure

```
mpato/
  __init__.py          # Public API surface
  loader.py            # Parses and validates service definition files
  protocols/
    rest.py            # Handles REST service calls via aiohttp
    wss.py             # Handles WebSocket services; runs listener in thread
  auth/
    resolver.py        # Resolves credential values from env, file, or static config
    injector.py        # Injects resolved credentials into requests
  dispatcher.py        # Routes calls to the correct protocol handler
  result.py            # Unified result/response type
  shims/
    mcp.py             # Client-side MCP compatibility layer over the registry
```

---

## Calling Convention

### Synchronous

```python
from mpato import ServiceRegistry

registry = ServiceRegistry()
registry.load("definitions/github.yaml")

result = registry.call("github", "get_issue", {"owner": "acme", "repo": "widget", "issue_number": 42})
print(result.data)
```

For REST: blocks until response received, returns `Result` object.
For WSS: blocks until end-of-message delimiter received or idle timeout exceeded, returns buffered `Result`.

### Asynchronous with callback

```python
def on_result(result):
    print(result.data)

registry.call_async("github", "get_issue", {"owner": "acme", "repo": "widget", "issue_number": 42}, callback=on_result)
```

### Asynchronous streaming (WSS only)

```python
pipe = registry.call_stream("my_wss_service", "subscribe", {"channel": "trades"})
for chunk in pipe:
    print(chunk)
```

Returns a pipe/iterator object. Does not block. Caller is responsible for consuming or closing the pipe.

---

## WebSocket Lifecycle

- On first call to a WSS service (or on explicit `.connect()`), the library opens a connection in a background thread and holds it open.
- Subsequent calls reuse the open connection.
- The background thread buffers inbound messages.
- Sync callers block on the buffer; async callers register a callback or receive a pipe.
- Connection teardown happens on explicit `.disconnect()` or when the registry is garbage collected.

---

## Auth Handling

Credentials are never hardcoded in definition files. The definition file specifies *how* to find the credential; the credential itself lives in the environment or a secrets file.

Supported resolution strategies:
- `env`: read from a named environment variable
- `file`: read from a file path (first line, stripped)
- `static`: value provided directly in the definition (for non-secret tokens only)

Supported injection strategies:
- `header`: inject as a named HTTP header
- `query`: inject as a query parameter
- `body`: inject into the request body

OAuth2 support: the definition file specifies the token endpoint and credential resolution for client ID/secret. The library handles the token exchange and caches the token, refreshing on expiry.

---

## Error Handling

All calls return a `Result` object with:
- `success: bool`
- `data: Any` — parsed response body on success
- `status_code: int` — HTTP status or None for WSS
- `error: str` — human-readable error description on failure
- `raw: bytes` — raw response for inspection

Exceptions are not raised by default. Callers can opt into exception-raising mode via a flag on the registry or per-call.

---

## Definition File Example

```yaml
name: github
base_url: https://api.github.com
protocol: rest
auth:
  type: bearer
  resolve:
    strategy: env
    key: GITHUB_TOKEN
  inject:
    strategy: header
    name: Authorization
    prefix: "Bearer "
endpoints:
  get_issue:
    path: /repos/{owner}/{repo}/issues/{issue_number}
    method: GET
    params:
      owner:
        type: string
        required: true
      repo:
        type: string
        required: true
      issue_number:
        type: integer
        required: true
    timeout_ms: 5000
```

---

## Non-Goals

- MPATO does not provide a server. There is no daemon, no port, no process to manage.
- MPATO does not implement a new protocol. It speaks existing protocols (HTTP, WebSocket, OAuth2) and nothing else.
- MPATO does not require modifications to target services.
- MPATO does not implement tool-use framing for any specific LLM in the core library. The MCP shim (`shims/mcp.py`) provides MCP-compatible tool schemas and dispatch as an optional layer; other LLM tool-use formats (OpenAI function calling, etc.) are out of scope for the core library but are natural consumers of it.

---

## MCP Compatibility Shim

MPATO optionally exposes a client-side MCP-compatible interface over any loaded service definitions. This requires no MCP server. The shim translates MPATO's registry into the MCP tool schema format and routes inbound `tool_use` requests back through the dispatcher.

### Usage

```python
from mpato import ServiceRegistry
from mpato.shims.mcp import MCPShim

registry = ServiceRegistry()
registry.load("definitions/github.yaml")

shim = MCPShim(registry)

# Get tool definitions to pass to an LLM
tools = shim.tools()  # returns MCP-compatible tool schema list

# Handle a tool_use block returned by the LLM
result = shim.dispatch(tool_use_block)  # returns MCP-compatible tool_result
```

### What the shim does

- `tools()` iterates loaded service definitions and emits a list of tool descriptors in MCP schema format — name, description, and input schema derived from the endpoint parameter definitions.
- `dispatch()` accepts an MCP `tool_use` block, resolves the target service and endpoint, and routes the call through `registry.call()`, returning the result in MCP `tool_result` format.
- Auth, retries, and protocol handling are unchanged — the shim is purely a translation layer over the existing dispatcher.

### What the shim does not do

- It does not run a server or open a port.
- It does not speak the MCP wire protocol over a socket — it operates in-process.
- It does not require or contact any external MCP server.

### Significance

Any agent framework with MCP tool-use support gets MPATO compatibility for free, without the framework needing to know MPATO exists. From the framework's perspective it receives tool schemas and dispatches tool calls through a standard interface. The shim handles all translation entirely on the client side. `shims/mcp.py` is MCP, implemented client-side, in one file, with no server.

---

## Relationship to MCP

MCP provides service discovery, auth, streaming, and tool invocation under one protocol. MPATO provides the same capabilities by composing existing infrastructure, and provides a client-side MCP compatibility shim for frameworks that expect MCP tool schemas:

| MCP capability | MPATO equivalent |
|---|---|
| Service discovery | Static definition files, hostable on CDN |
| Auth | Auth resolver + injector modules consuming existing OAuth2 / API key patterns |
| Streaming | Native WebSocket support in the WSS protocol handler |
| Tool invocation | `registry.call()` / `registry.call_async()` / `registry.call_stream()` |
| MCP tool schema | `shims/mcp.py` — `MCPShim.tools()` and `MCPShim.dispatch()` |
| Server-side adoption | Not required |

---

## Implementation Order

1. `loader.py` — definition file parsing and schema validation
2. `auth/resolver.py` and `auth/injector.py` — credential resolution and injection
3. `protocols/rest.py` — synchronous REST calls via `aiohttp`
4. `result.py` — unified result type
5. `dispatcher.py` — route calls through loader + protocol handler
6. `protocols/wss.py` — WebSocket listener thread, sync blocking, pipe interface
7. Async callback support across both protocol handlers
8. OAuth2 token exchange and caching in auth module
9. `shims/mcp.py` — `MCPShim.tools()` and `MCPShim.dispatch()` over the registry
10. Example definition files for at least two real public APIs
11. README examples validated against working code
