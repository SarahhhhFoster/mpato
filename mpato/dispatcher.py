"""
Routes calls through the appropriate protocol handler based on the service definition.
"""

from typing import Callable, Optional

from mpato.result import Result
from mpato.protocols.rest import RestHandler
from mpato.protocols.wss import WSSHandler, MessagePipe


class Dispatcher:
    """Routes calls to REST or WSS handlers."""

    def __init__(self):
        self._rest = RestHandler()
        self._wss = WSSHandler()

    def call(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
        raise_on_error: bool = False,
    ) -> Result:
        protocol = definition.get("protocol", "rest")
        if protocol == "rest":
            return self._rest.call(definition, endpoint_name, params, raise_on_error)
        elif protocol == "wss":
            return self._wss.call(definition, endpoint_name, params, raise_on_error)
        else:
            result = Result(success=False, error=f"Unsupported protocol: {protocol}")
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
        protocol = definition.get("protocol", "rest")
        if protocol == "rest":
            return self._rest.call_async(
                definition, endpoint_name, params, callback, raise_on_error
            )
        elif protocol == "wss":
            return self._wss.call_async(
                definition, endpoint_name, params, callback, raise_on_error
            )
        else:
            result = Result(success=False, error=f"Unsupported protocol: {protocol}")
            callback(result)

    def call_stream(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
    ) -> MessagePipe:
        protocol = definition.get("protocol", "rest")
        if protocol == "wss":
            return self._wss.call_stream(definition, endpoint_name, params)
        else:
            raise NotImplementedError("call_stream is only supported for WSS services")

    def connect(self, definition: dict):
        protocol = definition.get("protocol", "rest")
        if protocol == "wss":
            self._wss.connect(definition)

    def disconnect(self, definition: dict):
        protocol = definition.get("protocol", "rest")
        if protocol == "wss":
            self._wss.disconnect(definition)
