"""
Parses and validates MPATO service definition files (YAML or JSON).
Fails loudly at load time on invalid definitions.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


VALID_PROTOCOLS = {"rest", "wss"}
VALID_AUTH_TYPES = {"none", "apikey", "bearer", "oauth2"}
VALID_RESOLVE_STRATEGIES = {"env", "file", "static"}
VALID_INJECT_STRATEGIES = {"header", "query", "body"}
VALID_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
VALID_PARAM_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


class DefinitionError(Exception):
    """Raised when a service definition file is invalid."""
    pass


def _require(d: dict, key: str, context: str):
    if key not in d:
        raise DefinitionError(f"Missing required field '{key}' in {context}")
    return d[key]


def _validate_auth(auth: dict, service_name: str):
    auth_type = _require(auth, "type", f"auth in '{service_name}'").lower()
    if auth_type not in VALID_AUTH_TYPES:
        raise DefinitionError(
            f"Invalid auth type '{auth_type}' in '{service_name}'. "
            f"Must be one of: {VALID_AUTH_TYPES}"
        )
    auth["type"] = auth_type

    if auth_type == "none":
        return auth

    if auth_type in ("apikey", "bearer"):
        resolve = _require(auth, "resolve", f"auth in '{service_name}'")
        strategy = _require(resolve, "strategy", f"auth.resolve in '{service_name}'").lower()
        if strategy not in VALID_RESOLVE_STRATEGIES:
            raise DefinitionError(
                f"Invalid resolve strategy '{strategy}' in '{service_name}'. "
                f"Must be one of: {VALID_RESOLVE_STRATEGIES}"
            )
        resolve["strategy"] = strategy
        if strategy == "env":
            _require(resolve, "key", f"auth.resolve in '{service_name}'")
        elif strategy == "file":
            _require(resolve, "path", f"auth.resolve in '{service_name}'")
        elif strategy == "static":
            _require(resolve, "value", f"auth.resolve in '{service_name}'")

        inject = _require(auth, "inject", f"auth in '{service_name}'")
        inject_strategy = _require(inject, "strategy", f"auth.inject in '{service_name}'").lower()
        if inject_strategy not in VALID_INJECT_STRATEGIES:
            raise DefinitionError(
                f"Invalid inject strategy '{inject_strategy}' in '{service_name}'. "
                f"Must be one of: {VALID_INJECT_STRATEGIES}"
            )
        inject["strategy"] = inject_strategy
        if inject_strategy == "header":
            _require(inject, "name", f"auth.inject in '{service_name}'")

    elif auth_type == "oauth2":
        _require(auth, "token_url", f"auth (oauth2) in '{service_name}'")
        _require(auth, "client_id", f"auth (oauth2) in '{service_name}'")
        _require(auth, "client_secret", f"auth (oauth2) in '{service_name}'")

    return auth


def _validate_params(params: dict, endpoint_name: str, service_name: str):
    for param_name, param_def in params.items():
        if not isinstance(param_def, dict):
            raise DefinitionError(
                f"Param '{param_name}' in endpoint '{endpoint_name}' of '{service_name}' "
                f"must be a mapping"
            )
        p_type = param_def.get("type", "string")
        if p_type not in VALID_PARAM_TYPES:
            raise DefinitionError(
                f"Invalid param type '{p_type}' for '{param_name}' in "
                f"endpoint '{endpoint_name}' of '{service_name}'"
            )


def _validate_endpoint(name: str, ep: dict, protocol: str, service_name: str):
    if not isinstance(ep, dict):
        raise DefinitionError(
            f"Endpoint '{name}' in '{service_name}' must be a mapping"
        )
    path = _require(ep, "path", f"endpoint '{name}' in '{service_name}'")
    if not path.startswith("/"):
        raise DefinitionError(
            f"Endpoint path '{path}' in '{service_name}.{name}' must start with '/'"
        )

    if protocol == "rest":
        method = ep.get("method", "GET").upper()
        if method not in VALID_HTTP_METHODS:
            raise DefinitionError(
                f"Invalid method '{method}' for endpoint '{name}' in '{service_name}'"
            )
        ep["method"] = method

    if "params" in ep:
        _validate_params(ep["params"], name, service_name)


def load_definition(path: str) -> dict:
    """Load and validate a service definition file. Returns the parsed definition dict."""
    fpath = Path(path)
    if not fpath.exists():
        raise DefinitionError(f"Definition file not found: {path}")

    suffix = fpath.suffix.lower()
    raw = fpath.read_text(encoding="utf-8")

    if suffix in (".yaml", ".yml"):
        if not HAS_YAML:
            raise DefinitionError(
                "PyYAML is not installed. Install it with: pip install pyyaml"
            )
        try:
            definition = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise DefinitionError(f"YAML parse error in '{path}': {e}") from e
    elif suffix == ".json":
        try:
            definition = json.loads(raw)
        except json.JSONDecodeError as e:
            raise DefinitionError(f"JSON parse error in '{path}': {e}") from e
    else:
        raise DefinitionError(
            f"Unsupported definition file format '{suffix}'. Use .yaml, .yml, or .json"
        )

    if not isinstance(definition, dict):
        raise DefinitionError(f"Definition file '{path}' must be a YAML/JSON object")

    # Required top-level fields
    name = _require(definition, "name", f"definition '{path}'")
    _require(definition, "base_url", f"definition '{path}'")
    protocol = _require(definition, "protocol", f"definition '{path}'").lower()
    if protocol not in VALID_PROTOCOLS:
        raise DefinitionError(
            f"Invalid protocol '{protocol}' in '{path}'. Must be one of: {VALID_PROTOCOLS}"
        )
    definition["protocol"] = protocol

    # Auth (optional, defaults to none)
    if "auth" in definition:
        definition["auth"] = _validate_auth(definition["auth"], name)
    else:
        definition["auth"] = {"type": "none"}

    # Endpoints
    endpoints = definition.get("endpoints", {})
    if not isinstance(endpoints, dict):
        raise DefinitionError(f"'endpoints' in '{path}' must be a mapping")
    for ep_name, ep_def in endpoints.items():
        _validate_endpoint(ep_name, ep_def, protocol, name)

    definition["_source_path"] = str(fpath.resolve())
    return definition
