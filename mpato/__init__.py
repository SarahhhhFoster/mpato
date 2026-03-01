"""
MPATO — Multi-Protocol Agent Tool Operator

A client-side Python library for invoking external APIs (REST and WebSocket)
using static declarative definition files (YAML or JSON), with no
server-side requirements.

Usage:
    from mpato import ServiceRegistry

    registry = ServiceRegistry()
    registry.load("definitions/github.yaml")

    result = registry.call("github", "get_issue", {
        "owner": "acme", "repo": "widget", "issue_number": 42
    })
    print(result.data)
"""

from mpato.loader import load_definition, DefinitionError
from mpato.dispatcher import Dispatcher
from mpato.result import Result
from mpato.protocols.wss import MessagePipe
from mpato.discovery import discover, DiscoveryError, ServiceEntry

__all__ = ["ServiceRegistry", "Result", "MessagePipe", "DefinitionError", "discover", "DiscoveryError", "ServiceEntry"]
__version__ = "0.1.0"


class ServiceRegistry:
    """
    Central registry for MPATO service definitions.

    Load definitions from YAML/JSON files, then call endpoints by name.
    Supports synchronous, async callback, and streaming (WSS) patterns.
    """

    def __init__(self, raise_on_error: bool = False):
        """
        Args:
            raise_on_error: If True, all calls raise RuntimeError on failure
                            instead of returning a Result with success=False.
        """
        self._definitions: dict = {}
        self._dispatcher = Dispatcher()
        self.raise_on_error = raise_on_error

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, path: str) -> "ServiceRegistry":
        """
        Load a service definition file.

        Args:
            path: Path to a YAML or JSON definition file.

        Returns:
            self (for chaining)

        Raises:
            DefinitionError: If the file is missing, malformed, or invalid.
        """
        definition = load_definition(path)
        name = definition["name"]
        self._definitions[name] = definition
        return self

    def load_all(self, paths) -> "ServiceRegistry":
        """Load multiple definition files."""
        for path in paths:
            self.load(path)
        return self

    def load_index(
        self,
        index_source: str,
        tags=None,
        auth_types=None,
    ) -> "ServiceRegistry":
        """
        Discover and load all services listed in an mpato-index.json file.

        The index may be a local path or an http/https URL. Definition files
        referenced by the index may also be local paths or URLs.

        Args:
            index_source: Path or URL to an mpato-index.json.
            tags:         Optional list of tags to filter by (OR match).
            auth_types:   Optional list of auth types to filter by.

        Returns:
            self (for chaining)

        Raises:
            DiscoveryError: If the index is missing, malformed, or a
                            definition cannot be resolved.
        """
        from mpato.discovery import discover
        entries = discover(index_source, tags=tags, auth_types=auth_types)
        for entry in entries:
            self.load(entry.local_path)
        return self


    def services(self) -> list:
        """Return a list of loaded service names."""
        return list(self._definitions.keys())

    def definition(self, service_name: str) -> dict:
        """Return the raw definition dict for a service."""
        self._require_service(service_name)
        return self._definitions[service_name]

    # ------------------------------------------------------------------
    # Calling
    # ------------------------------------------------------------------

    def call(
        self,
        service_name: str,
        endpoint_name: str,
        params: dict = None,
        raise_on_error: bool = None,
    ) -> Result:
        """
        Synchronously call a service endpoint.

        For REST: blocks until response received.
        For WSS: blocks until end-of-message delimiter or idle timeout.

        Args:
            service_name:  Name of the loaded service.
            endpoint_name: Name of the endpoint within the service.
            params:        Parameters to pass to the endpoint.
            raise_on_error: Override the registry-level raise_on_error setting.

        Returns:
            Result object with .success, .data, .status_code, .error, .raw
        """
        self._require_service(service_name)
        should_raise = self.raise_on_error if raise_on_error is None else raise_on_error
        return self._dispatcher.call(
            self._definitions[service_name],
            endpoint_name,
            params or {},
            raise_on_error=should_raise,
        )

    def call_async(
        self,
        service_name: str,
        endpoint_name: str,
        params: dict = None,
        callback=None,
        raise_on_error: bool = None,
    ):
        """
        Call a service endpoint asynchronously.

        The callback is invoked with the Result when the call completes.

        Args:
            service_name:  Name of the loaded service.
            endpoint_name: Name of the endpoint within the service.
            params:        Parameters to pass to the endpoint.
            callback:      Callable(Result) invoked on completion.
            raise_on_error: Override the registry-level raise_on_error setting.

        Returns:
            Threading handle (thread object).
        """
        self._require_service(service_name)
        should_raise = self.raise_on_error if raise_on_error is None else raise_on_error

        def _default_callback(result):
            pass

        return self._dispatcher.call_async(
            self._definitions[service_name],
            endpoint_name,
            params or {},
            callback=callback or _default_callback,
            raise_on_error=should_raise,
        )

    def call_stream(
        self,
        service_name: str,
        endpoint_name: str,
        params: dict = None,
    ) -> MessagePipe:
        """
        Call a WSS endpoint and return a streaming pipe.

        Non-blocking. Returns a MessagePipe iterator. Caller is responsible
        for consuming or closing the pipe.

        Args:
            service_name:  Name of the loaded WSS service.
            endpoint_name: Name of the endpoint within the service.
            params:        Parameters to pass to the endpoint.

        Returns:
            MessagePipe — iterate with `for chunk in pipe:`

        Raises:
            NotImplementedError: If called on a REST service.
        """
        self._require_service(service_name)
        return self._dispatcher.call_stream(
            self._definitions[service_name],
            endpoint_name,
            params or {},
        )

    # ------------------------------------------------------------------
    # Connection management (WSS)
    # ------------------------------------------------------------------

    def connect(self, service_name: str) -> "ServiceRegistry":
        """
        Explicitly open a WebSocket connection for a WSS service.
        Connections are also opened automatically on first call.
        """
        self._require_service(service_name)
        self._dispatcher.connect(self._definitions[service_name])
        return self

    def disconnect(self, service_name: str) -> "ServiceRegistry":
        """Close the WebSocket connection for a WSS service."""
        self._require_service(service_name)
        self._dispatcher.disconnect(self._definitions[service_name])
        return self

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_service(self, service_name: str):
        if service_name not in self._definitions:
            raise KeyError(
                f"Service '{service_name}' not loaded. "
                f"Available services: {list(self._definitions.keys())}"
            )

    # ------------------------------------------------------------------
    # Context manager + explicit close
    # ------------------------------------------------------------------

    def close(self) -> None:
        """
        Explicitly close all open WebSocket connections.
        Prefer this over relying on garbage collection.
        """
        for name in list(self._definitions.keys()):
            defn = self._definitions.get(name, {})
            if defn.get("protocol") == "wss":
                try:
                    self._dispatcher.disconnect(defn)
                except Exception:
                    pass

    def __enter__(self) -> "ServiceRegistry":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __del__(self):
        # Best-effort fallback; prefer explicit close() or context manager.
        try:
            self.close()
        except Exception:
            pass
