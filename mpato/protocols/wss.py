"""
WebSocket protocol handler for MPATO.

Runs a persistent connection in a background thread. Each call registers a
*correlated* subscriber: the endpoint definition may specify a
``correlation.request_id_field`` (field name to inject into outgoing messages)
and a ``correlation.response_id_field`` (field name to match in inbound JSON
messages). When correlation is configured, each call gets only the messages
intended for it; uncorrelated calls fall back to delimiter/idle-timeout
buffering on all inbound messages.

Concurrency model
─────────────────
- One background thread per service runs run_forever().
- on_message() dispatches each inbound message to every registered Subscriber
  whose predicate matches (or to all subscribers if no predicate).
- Subscribers register before the outbound message is sent, so no messages
  are missed.
- Disconnect/close sends _SENTINEL to all pending subscribers so they unblock.
- MessagePipe.close() unregisters the subscriber and drains the queue.
"""

import json
import queue
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from mpato.result import Result
from mpato.auth.resolver import resolve_credential
from mpato.auth.injector import inject_credential

try:
    import websocket as _ws
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


_SENTINEL = object()  # end-of-stream marker


# ─────────────────────────────────────────────────────────────────────────────
# Subscriber
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Subscriber:
    q: queue.Queue = field(default_factory=queue.Queue)
    predicate: Optional[Callable[[str], bool]] = None  # None = accept all

    def accepts(self, message: str) -> bool:
        if self.predicate is None:
            return True
        try:
            return self.predicate(message)
        except Exception:
            return False

    def deliver(self, message: str):
        self.q.put(message)

    def close(self):
        self.q.put(_SENTINEL)


# ─────────────────────────────────────────────────────────────────────────────
# MessagePipe
# ─────────────────────────────────────────────────────────────────────────────

class MessagePipe:
    """
    Iterator returned by call_stream(). Yields raw message strings until the
    connection closes or close() is called.

    Supports use as a context manager:

        with registry.call_stream("svc", "subscribe", {...}) as pipe:
            for chunk in pipe:
                process(chunk)
    """

    def __init__(self, subscriber: _Subscriber, conn: "_WSSConnection"):
        self._sub = subscriber
        self._conn = conn
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        item = self._sub.q.get()
        if item is _SENTINEL:
            self._closed = True
            raise StopIteration
        return item

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if not self._closed:
            self._closed = True
            self._conn.unsubscribe(self._sub)
            # Send sentinel so any blocked .get() in __next__ unblocks
            self._sub.close()
            # Drain
            while not self._sub.q.empty():
                try:
                    self._sub.q.get_nowait()
                except queue.Empty:
                    break


# ─────────────────────────────────────────────────────────────────────────────
# _WSSConnection
# ─────────────────────────────────────────────────────────────────────────────

class _WSSConnection:
    """Manages a single persistent WebSocket connection with subscriber fanout."""

    def __init__(self, url: str, headers: dict):
        self.url = url
        self.headers = headers
        self._ws_app = None
        self._thread = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._subscribers: List[_Subscriber] = []
        self._running = False

    def connect(self):
        if self._running:
            return
        self._running = True

        ws_headers = [f"{k}: {v}" for k, v in self.headers.items()]

        def on_open(ws):
            self._connected.set()

        def on_message(ws, message):
            with self._lock:
                subs = list(self._subscribers)
            for sub in subs:
                if sub.accepts(message):
                    sub.deliver(message)

        def on_error(ws, error):
            pass  # run_forever handles reconnect; subscribers unblock on close

        def on_close(ws, close_status_code, close_msg):
            self._connected.clear()
            with self._lock:
                subs = list(self._subscribers)
                self._subscribers.clear()
            for sub in subs:
                sub.close()

        self._ws_app = _ws.WebSocketApp(
            self.url,
            header=ws_headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        self._thread = threading.Thread(
            target=self._ws_app.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        self._thread.start()
        self._connected.wait(timeout=10)

    def send(self, payload: Any):
        if not self._ws_app or not self.is_connected:
            raise RuntimeError("WebSocket not connected")
        if isinstance(payload, dict):
            self._ws_app.send(json.dumps(payload))
        else:
            self._ws_app.send(str(payload))

    def subscribe(self, predicate: Optional[Callable[[str], bool]] = None) -> _Subscriber:
        sub = _Subscriber(predicate=predicate)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber):
        with self._lock:
            try:
                self._subscribers.remove(sub)
            except ValueError:
                pass

    def disconnect(self):
        self._running = False
        if self._ws_app:
            self._ws_app.close()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()


# ─────────────────────────────────────────────────────────────────────────────
# Correlation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_correlation_predicate(response_id_field: str, request_id: str):
    """Return a predicate that accepts only messages where response_id_field == request_id."""
    def predicate(message: str) -> bool:
        try:
            data = json.loads(message)
            return str(data.get(response_id_field)) == str(request_id)
        except Exception:
            return False
    return predicate


def _inject_request_id(message: Any, request_id_field: str, request_id: str) -> Any:
    if isinstance(message, dict):
        return {**message, request_id_field: request_id}
    return message  # non-dict messages can't carry structured correlation IDs


def _get_correlation_config(definition: dict, endpoint: dict):
    """Returns (request_id_field, response_id_field) or (None, None)."""
    corr = endpoint.get("correlation") or definition.get("correlation") or {}
    req_field = corr.get("request_id_field")
    resp_field = corr.get("response_id_field")
    if req_field and resp_field:
        return req_field, resp_field
    return None, None


# ─────────────────────────────────────────────────────────────────────────────
# WSSHandler
# ─────────────────────────────────────────────────────────────────────────────

class WSSHandler:
    """Handles WebSocket service calls with per-call correlation."""

    def __init__(self):
        self._connections: Dict[str, _WSSConnection] = {}
        self._lock = threading.Lock()

    def _resolve_auth_headers(self, definition: dict) -> dict:
        auth = definition.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")
        headers = {}
        if auth_type in ("bearer", "apikey"):
            resolve_config = auth.get("resolve", {})
            credential = resolve_credential(resolve_config, definition["name"])
            inject_config = auth.get("inject", {})
            headers, _, _ = inject_credential(credential, inject_config, headers, {}, {})
        return headers

    def _get_connection(self, definition: dict) -> _WSSConnection:
        name = definition["name"]
        with self._lock:
            conn = self._connections.get(name)
            if conn and conn.is_connected:
                return conn
            headers = self._resolve_auth_headers(definition)
            conn = _WSSConnection(url=definition["base_url"], headers=headers)
            conn.connect()
            self._connections[name] = conn
            return conn

    def connect(self, definition: dict):
        self._get_connection(definition)

    def disconnect(self, definition: dict):
        name = definition["name"]
        with self._lock:
            conn = self._connections.pop(name, None)
        if conn:
            conn.disconnect()

    def _build_message(self, endpoint: dict, params: dict) -> Any:
        message_template = endpoint.get("message")
        if message_template:
            if isinstance(message_template, dict):
                return {**message_template, **params}
            return str(message_template)
        return dict(params)

    def call(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
        raise_on_error: bool = False,
    ) -> Result:
        """
        Send a WSS message and block until end-of-message delimiter or idle timeout.
        Uses correlation if the endpoint/service defines it.
        """
        if not HAS_WEBSOCKET:
            result = Result(
                success=False,
                error="websocket-client library required. Install: pip install websocket-client",
            )
            if raise_on_error:
                result.raise_for_error()
            return result

        endpoint = definition.get("endpoints", {}).get(endpoint_name)
        if endpoint is None:
            result = Result(
                success=False,
                error=f"Unknown endpoint '{endpoint_name}' in '{definition['name']}'",
            )
            if raise_on_error:
                result.raise_for_error()
            return result

        streaming = definition.get("streaming", {})
        delimiter = streaming.get("end_of_message_delimiter")
        idle_timeout_ms = streaming.get("idle_timeout_ms", 2000)
        idle_timeout_s = idle_timeout_ms / 1000.0

        try:
            conn = self._get_connection(definition)
            message = self._build_message(endpoint, params)

            req_field, resp_field = _get_correlation_config(definition, endpoint)
            predicate = None
            if req_field and resp_field:
                request_id = str(uuid.uuid4())
                message = _inject_request_id(message, req_field, request_id)
                predicate = _make_correlation_predicate(resp_field, request_id)

            # Subscribe before sending — no missed messages
            sub = conn.subscribe(predicate=predicate)
            try:
                conn.send(message)
                buffer = []
                while True:
                    try:
                        chunk = sub.q.get(timeout=idle_timeout_s)
                        if chunk is _SENTINEL:
                            break
                        buffer.append(chunk)
                        if delimiter and chunk.endswith(delimiter):
                            break
                    except queue.Empty:
                        break
            finally:
                conn.unsubscribe(sub)

            raw_text = "".join(buffer)
            try:
                data = json.loads(raw_text)
            except Exception:
                data = raw_text

            return Result(success=True, data=data, raw=raw_text.encode())

        except Exception as e:
            result = Result(success=False, error=str(e))
            if raise_on_error:
                result.raise_for_error()
            return result

    def call_async(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
        callback: Callable[[Result], None],
        raise_on_error: bool = False,
    ):
        def _run():
            result = self.call(definition, endpoint_name, params, raise_on_error=False)
            callback(result)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def call_stream(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
    ) -> MessagePipe:
        """
        Send a WSS message and return a MessagePipe. Non-blocking.
        Use as a context manager or call .close() when done.
        """
        def _empty_pipe():
            sub = _Subscriber()
            sub.close()
            return MessagePipe(sub, _WSSConnection("", {}))

        if not HAS_WEBSOCKET:
            return _empty_pipe()

        endpoint = definition.get("endpoints", {}).get(endpoint_name)
        if endpoint is None:
            return _empty_pipe()

        conn = self._get_connection(definition)
        message = self._build_message(endpoint, params)

        req_field, resp_field = _get_correlation_config(definition, endpoint)
        predicate = None
        if req_field and resp_field:
            request_id = str(uuid.uuid4())
            message = _inject_request_id(message, req_field, request_id)
            predicate = _make_correlation_predicate(resp_field, request_id)

        sub = conn.subscribe(predicate=predicate)
        conn.send(message)
        return MessagePipe(sub, conn)
