"""
REST protocol handler for MPATO.
Handles synchronous and async REST service calls.
"""

import json
import threading
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode

from mpato.result import Result
from mpato.auth.resolver import resolve_credential
from mpato.auth.injector import inject_credential
from mpato.auth.oauth2 import get_oauth2_token

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class RestHandler:
    """Handles REST (HTTP) service calls."""

    def _resolve_auth(self, definition: dict) -> tuple:
        """Returns (headers, params_extra) with auth injected."""
        auth = definition.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")

        headers = {}
        params_extra = {}
        body_extra = {}

        if auth_type == "none":
            return headers, params_extra, body_extra

        if auth_type in ("bearer", "apikey"):
            resolve_config = auth.get("resolve", {})
            credential = resolve_credential(resolve_config, definition["name"])
            inject_config = auth.get("inject", {})
            headers, params_extra, body_extra = inject_credential(
                credential, inject_config, headers, params_extra, {}
            )

        elif auth_type == "oauth2":
            token = get_oauth2_token(auth, definition["name"])
            inject_config = auth.get("inject", {
                "strategy": "header",
                "name": "Authorization",
                "prefix": "Bearer ",
            })
            headers, params_extra, body_extra = inject_credential(
                token, inject_config, headers, params_extra, {}
            )

        return headers, params_extra, body_extra

    def _build_url(self, base_url: str, path: str, path_params: dict) -> str:
        """Interpolate path parameters into the URL."""
        url = base_url.rstrip("/") + path
        for key, value in path_params.items():
            url = url.replace(f"{{{key}}}", str(value))
        return url

    def _split_params(self, endpoint: dict, call_params: dict):
        """
        Split call_params into path_params, query_params, and body_params
        based on endpoint param definitions.
        """
        param_defs = endpoint.get("params", {})
        method = endpoint.get("method", "GET").upper()
        path = endpoint.get("path", "")

        path_keys = set()
        import re
        for match in re.finditer(r"\{(\w+)\}", path):
            path_keys.add(match.group(1))

        path_params = {}
        query_params = {}
        body_params = {}

        for key, value in call_params.items():
            if key in path_keys:
                path_params[key] = value
            elif method in ("GET", "HEAD", "DELETE"):
                query_params[key] = value
            else:
                body_params[key] = value

        return path_params, query_params, body_params

    def call(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
        raise_on_error: bool = False,
    ) -> Result:
        """Execute a synchronous REST call."""
        if not HAS_REQUESTS:
            result = Result(
                success=False,
                error="requests library is required. Install with: pip install requests",
            )
            if raise_on_error:
                result.raise_for_error()
            return result

        endpoint = definition.get("endpoints", {}).get(endpoint_name)
        if endpoint is None:
            result = Result(
                success=False,
                error=f"Unknown endpoint '{endpoint_name}' in service '{definition['name']}'",
            )
            if raise_on_error:
                result.raise_for_error()
            return result

        # Validate required params
        for param_name, param_def in endpoint.get("params", {}).items():
            if param_def.get("required") and param_name not in params:
                result = Result(
                    success=False,
                    error=f"Required parameter '{param_name}' missing for "
                          f"'{definition['name']}.{endpoint_name}'",
                )
                if raise_on_error:
                    result.raise_for_error()
                return result

        # Auth
        try:
            auth_headers, auth_query, auth_body = self._resolve_auth(definition)
        except Exception as e:
            result = Result(success=False, error=f"Auth error: {e}")
            if raise_on_error:
                result.raise_for_error()
            return result

        method = endpoint.get("method", "GET").upper()
        timeout_ms = endpoint.get("timeout_ms") or definition.get("timeout_ms") or 10000
        timeout_s = timeout_ms / 1000.0

        path_params, query_params, body_params = self._split_params(endpoint, params)
        query_params.update(auth_query)
        body_params.update(auth_body)

        url = self._build_url(definition["base_url"], endpoint["path"], path_params)

        try:
            resp = _requests.request(
                method=method,
                url=url,
                headers=auth_headers,
                params=query_params if query_params else None,
                json=body_params if body_params and method not in ("GET", "HEAD") else None,
                timeout=timeout_s,
            )
            raw = resp.content
            status_code = resp.status_code
            success = resp.ok

            try:
                data = resp.json()
            except Exception:
                data = resp.text

            error = None if success else f"HTTP {status_code}: {resp.reason}"

            result = Result(
                success=success,
                data=data,
                status_code=status_code,
                error=error,
                raw=raw,
            )
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
        """Execute a REST call asynchronously, invoking callback with the Result."""
        def _run():
            result = self.call(definition, endpoint_name, params, raise_on_error=False)
            callback(result)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t
