"""
MCP Compatibility Shim for MPATO.

Exposes MPATO service definitions as MCP-compatible tool schemas,
and routes MCP tool_use blocks back through the MPATO dispatcher.
All translation happens in-process — no server, no port, no MCP wire protocol.

Usage:
    from mpato import ServiceRegistry
    from mpato.shims.mcp import MCPShim

    registry = ServiceRegistry()
    registry.load("definitions/github.yaml")

    shim = MCPShim(registry)

    # Get tool definitions to pass to an LLM
    tools = shim.tools()  # MCP-compatible tool schema list

    # Handle a tool_use block returned by the LLM
    result = shim.dispatch(tool_use_block)  # MCP-compatible tool_result
"""

import json
from typing import Any, Dict, List, Optional


# Mapping from MPATO param types to JSON Schema types
_TYPE_MAP = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}


class MCPShim:
    """
    Client-side MCP compatibility layer over the MPATO ServiceRegistry.

    Provides tools() and dispatch() methods matching the MCP tool-use interface.
    """

    def __init__(self, registry):
        """
        Args:
            registry: An mpato.ServiceRegistry instance with loaded definitions.
        """
        self._registry = registry

    def tools(self) -> List[Dict[str, Any]]:
        """
        Return a list of MCP-compatible tool schema objects for all loaded endpoints.

        Each tool corresponds to one endpoint across all loaded services.
        Tool names are formatted as "<service_name>__<endpoint_name>" to ensure
        global uniqueness across services.

        Returns:
            List of dicts in MCP tool schema format:
            [
                {
                    "name": "github__get_issue",
                    "description": "...",
                    "input_schema": {
                        "type": "object",
                        "properties": {...},
                        "required": [...]
                    }
                },
                ...
            ]
        """
        tool_list = []

        for service_name in self._registry.services():
            definition = self._registry.definition(service_name)
            base_url = definition.get("base_url", "")
            protocol = definition.get("protocol", "rest")

            for endpoint_name, endpoint in definition.get("endpoints", {}).items():
                tool_name = f"{service_name}__{endpoint_name}"

                # Build description from available metadata
                description_parts = []
                if endpoint.get("description"):
                    description_parts.append(endpoint["description"])
                else:
                    method = endpoint.get("method", "").upper()
                    path = endpoint.get("path", "")
                    if method and path:
                        description_parts.append(f"{method} {base_url}{path}")
                    elif path:
                        description_parts.append(f"{protocol.upper()} {path}")
                    else:
                        description_parts.append(f"Call {endpoint_name} on {service_name}")

                description = " — ".join(description_parts) if description_parts else f"Call {endpoint_name}"

                # Build JSON Schema for input_schema
                properties = {}
                required = []

                for param_name, param_def in endpoint.get("params", {}).items():
                    prop = {
                        "type": _TYPE_MAP.get(param_def.get("type", "string"), "string"),
                    }
                    if param_def.get("description"):
                        prop["description"] = param_def["description"]
                    if param_def.get("enum"):
                        prop["enum"] = param_def["enum"]
                    if param_def.get("default") is not None:
                        prop["default"] = param_def["default"]
                    properties[param_name] = prop

                    if param_def.get("required", False):
                        required.append(param_name)

                input_schema = {
                    "type": "object",
                    "properties": properties,
                }
                if required:
                    input_schema["required"] = required

                tool_list.append({
                    "name": tool_name,
                    "description": description,
                    "input_schema": input_schema,
                })

        return tool_list

    def dispatch(self, tool_use_block: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle an MCP tool_use block and return an MCP tool_result dict.

        Expects a tool_use block in the format:
            {
                "type": "tool_use",
                "id": "<tool_use_id>",
                "name": "<service_name>__<endpoint_name>",
                "input": { ... }
            }

        Returns an MCP tool_result in the format:
            {
                "type": "tool_result",
                "tool_use_id": "<tool_use_id>",
                "content": "<result_as_json_string>",
                "is_error": false
            }
        """
        tool_use_id = tool_use_block.get("id", "unknown")
        tool_name = tool_use_block.get("name", "")
        params = tool_use_block.get("input", {})

        # Parse service and endpoint names
        if "__" not in tool_name:
            return self._error_result(
                tool_use_id,
                f"Invalid tool name '{tool_name}'. Expected format: '<service>__<endpoint>'",
            )

        # Split on first __ only, in case endpoint name contains __
        parts = tool_name.split("__", 1)
        service_name, endpoint_name = parts[0], parts[1]

        # Dispatch through registry
        try:
            result = self._registry.call(service_name, endpoint_name, params)
        except KeyError as e:
            return self._error_result(tool_use_id, str(e))
        except Exception as e:
            return self._error_result(tool_use_id, f"Unexpected error: {e}")

        if result.success:
            content = json.dumps(result.data) if not isinstance(result.data, str) else result.data
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": False,
            }
        else:
            return self._error_result(tool_use_id, result.error or "Call failed")

    @staticmethod
    def _error_result(tool_use_id: str, error_message: str) -> Dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": json.dumps({"error": error_message}),
            "is_error": True,
        }
