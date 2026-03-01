"""
WebSocket protocol handler for MPATO.
Runs a persistent connection in a background thread with buffer management.
"""

import json
import queue
import threading
import time
from typing import Any, Callable, Dict, Iterator, Optional

from mpato.result import Result
from mpato.auth.resolver import resolve_credential
from mpato.auth.injector import inject_credential

try:
    import websocket as _ws
    HAS_WEBSOCKET = True
except ImportError:
    HAS_WEBSOCKET = False


_SENTINEL = object()  # marks end of stream


class MessagePipe:
    """
    Iterator/pipe returned by call_stream(). Yields chunks until the
    end-of-message delimiter is received or the connection is closed.
    """

    def __init__(self, chunk_queue: queue.Queue):
        self._queue = chunk_queue
        self._closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed:
            raise StopIteration
        item = self._queue.get()
        if item is _SENTINEL:
            self._closed = True
            raise StopIteration
        return item

    def close(self):
        self._closed = True
        # Drain queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


class _WSSConnection:
    """Manages a single persistent WebSocket connection."""

    def __init__(self, url: str, headers: dict, on_message_cb=None):
        self.url = url
        self.headers = headers
        self._ws_app = None
        self._thread = None
        self._connected = threading.Event()
        self._lock = threading.Lock()
        self._subscribers: list = []  # list of queue.Queue
        self._on_message_cb = on_message_cb
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
            for q in subs:
                q.put(message)
            if self._on_message_cb:
                self._on_message_cb(message)

        def on_error(ws, error):
            pass  # connection errors are handled by reconnect logic

        def on_close(ws, close_status_code, close_msg):
            self._connected.clear()
            with self._lock:
                subs = list(self._subscribers)
            for q in subs:
                q.put(_SENTINEL)

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
        if not self._ws_app:
            raise RuntimeError("WebSocket not connected")
        if isinstance(payload, dict):
            self._ws_app.send(json.dumps(payload))
        else:
            self._ws_app.send(str(payload))

    def subscribe(self) -> queue.Queue:
        q = queue.Queue()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def disconnect(self):
        self._running = False
        if self._ws_app:
            self._ws_app.close()

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()


class WSSHandler:
    """Handles WebSocket service calls."""

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
            # Create new connection
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
        """Build the message payload for a WSS endpoint call."""
        message_template = endpoint.get("message")
        if message_template:
            if isinstance(message_template, dict):
                msg = dict(message_template)
                msg.update(params)
                return msg
            return str(message_template)
        return params

    def call(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
        raise_on_error: bool = False,
    ) -> Result:
        """
        Send a WSS message and block until end-of-message delimiter or idle timeout.
        Returns a buffered Result.
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
            q = conn.subscribe()
            message = self._build_message(endpoint, params)
            conn.send(message)

            buffer = []
            try:
                while True:
                    try:
                        chunk = q.get(timeout=idle_timeout_s)
                        if chunk is _SENTINEL:
                            break
                        buffer.append(chunk)
                        if delimiter and chunk.endswith(delimiter):
                            break
                    except queue.Empty:
                        # Idle timeout — consider message complete
                        break
            finally:
                conn.unsubscribe(q)

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
        """Execute a WSS call asynchronously, invoking callback with the Result."""
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
        Send a WSS message and return a MessagePipe that yields raw chunks.
        Non-blocking. Caller is responsible for consuming or closing the pipe.
        """
        if not HAS_WEBSOCKET:
            # Return a pipe that immediately ends
            q = queue.Queue()
            q.put(_SENTINEL)
            return MessagePipe(q)

        endpoint = definition.get("endpoints", {}).get(endpoint_name)
        if endpoint is None:
            q = queue.Queue()
            q.put(_SENTINEL)
            return MessagePipe(q)

        conn = self._get_connection(definition)
        q = conn.subscribe()
        message = self._build_message(endpoint, params)
        conn.send(message)
        return MessagePipe(q)
