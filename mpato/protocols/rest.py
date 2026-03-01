"""
REST protocol handler for MPATO.

Parameter location and body encoding
─────────────────────────────────────
Each parameter definition may carry an optional ``in`` field:

    params:
      q:
        type: string
        in: query          # always a query param, even on POST
      file:
        type: string
        in: body           # always in the body
      user_id:
        type: integer
        in: path           # interpolated into the URL path
      x-custom:
        type: string
        in: header         # sent as a request header

When ``in`` is absent the old inference rules apply:
  - path placeholder → path
  - GET / HEAD / DELETE → query
  - everything else → body

Body encoding is controlled by the endpoint-level ``body_encoding`` field:

    body_encoding: json        # default — application/json
    body_encoding: form        # application/x-www-form-urlencoded
    body_encoding: multipart   # multipart/form-data (file uploads)

Retry policy
────────────
Services and endpoints may specify a ``retry`` block:

    retry:
      max_attempts: 3
      backoff_base_ms: 200     # wait = backoff_base_ms * 2^(attempt-1)
      retry_on: [429, 500, 502, 503, 504]

A 401 response also triggers a token refresh when OAuth2 is in use, then
retries once.
"""

import re
import threading
import time
from typing import Any, Callable, Dict, Optional

from mpato.result import Result
from mpato.auth.resolver import resolve_credential
from mpato.auth.injector import inject_credential
from mpato.auth.oauth2 import get_oauth2_token, invalidate_oauth2_token

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")

# Valid explicit ``in`` values
_VALID_IN = {"path", "query", "body", "header"}
_BODY_ENCODINGS = {"json", "form", "multipart"}


class RestHandler:

    # ── Auth ─────────────────────────────────────────────────────────────────

    def _resolve_auth(self, definition: dict, force_refresh: bool = False):
        """Returns (headers, query_extra, body_extra)."""
        auth = definition.get("auth", {"type": "none"})
        auth_type = auth.get("type", "none")

        headers, query_extra, body_extra = {}, {}, {}

        if auth_type == "none":
            return headers, query_extra, body_extra

        if auth_type in ("bearer", "apikey"):
            cred = resolve_credential(auth.get("resolve", {}), definition["name"])
            headers, query_extra, body_extra = inject_credential(
                cred, auth.get("inject", {}), headers, query_extra, {}
            )

        elif auth_type == "oauth2":
            if force_refresh:
                invalidate_oauth2_token(auth, definition["name"])
            token = get_oauth2_token(auth, definition["name"])
            inject_cfg = auth.get("inject", {
                "strategy": "header",
                "name": "Authorization",
                "prefix": "Bearer ",
            })
            headers, query_extra, body_extra = inject_credential(
                token, inject_cfg, headers, query_extra, {}
            )

        return headers, query_extra, body_extra

    # ── Param routing ─────────────────────────────────────────────────────────

    def _route_params(self, endpoint: dict, call_params: dict):
        """
        Route call_params into path_params, query_params, body_params,
        and header_params based on explicit ``in`` declarations or inference.
        """
        param_defs = endpoint.get("params", {})
        method = endpoint.get("method", "GET").upper()
        path = endpoint.get("path", "")
        path_keys = {m.group(1) for m in _PATH_PARAM_RE.finditer(path)}

        path_params, query_params, body_params, header_params = {}, {}, {}, {}

        for key, value in call_params.items():
            param_def = param_defs.get(key, {})
            explicit_in = param_def.get("in", "").lower()

            if explicit_in == "path" or (not explicit_in and key in path_keys):
                path_params[key] = value
            elif explicit_in == "query":
                query_params[key] = value
            elif explicit_in == "body":
                body_params[key] = value
            elif explicit_in == "header":
                header_params[key] = value
            elif explicit_in and explicit_in not in _VALID_IN:
                raise ValueError(
                    f"Invalid 'in' value '{explicit_in}' for param '{key}' "
                    f"in endpoint '{endpoint.get('path', '')}'"
                )
            else:
                # Inference: no explicit location
                if method in ("GET", "HEAD", "DELETE"):
                    query_params[key] = value
                else:
                    body_params[key] = value

        return path_params, query_params, body_params, header_params

    def _build_url(self, base_url: str, path: str, path_params: dict) -> str:
        url = base_url.rstrip("/") + path
        for key, value in path_params.items():
            url = url.replace(f"{{{key}}}", str(value))
        return url

    # ── Retry policy ─────────────────────────────────────────────────────────

    def _retry_config(self, definition: dict, endpoint: dict) -> dict:
        default = {"max_attempts": 1, "backoff_base_ms": 200, "retry_on": []}
        svc_retry = definition.get("retry", {})
        ep_retry = endpoint.get("retry", {})
        merged = {**default, **svc_retry, **ep_retry}
        return merged

    # ── Core call ─────────────────────────────────────────────────────────────

    def _execute(
        self,
        definition: dict,
        endpoint: dict,
        endpoint_name: str,
        params: dict,
        force_refresh: bool = False,
    ) -> Result:
        try:
            auth_headers, auth_query, auth_body = self._resolve_auth(
                definition, force_refresh=force_refresh
            )
        except Exception as e:
            return Result(success=False, error=f"Auth error: {e}")

        method = endpoint.get("method", "GET").upper()
        timeout_ms = endpoint.get("timeout_ms") or definition.get("timeout_ms") or 10000
        timeout_s = timeout_ms / 1000.0
        body_encoding = endpoint.get("body_encoding", "json").lower()

        path_params, query_params, body_params, header_params = self._route_params(
            endpoint, params
        )
        query_params.update(auth_query)
        body_params.update(auth_body)

        headers = {**auth_headers, **header_params}
        url = self._build_url(definition["base_url"], endpoint["path"], path_params)

        # Body encoding
        req_kwargs: dict = {
            "method": method,
            "url": url,
            "headers": headers,
            "params": query_params or None,
            "timeout": timeout_s,
        }
        if body_params and method not in ("GET", "HEAD"):
            if body_encoding == "form":
                req_kwargs["data"] = body_params
            elif body_encoding == "multipart":
                req_kwargs["files"] = {k: (None, str(v)) for k, v in body_params.items()}
            else:
                req_kwargs["json"] = body_params

        try:
            resp = _requests.request(**req_kwargs)
            raw = resp.content
            status_code = resp.status_code
            success = resp.ok
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            error = None if success else f"HTTP {status_code}: {resp.reason}"
            return Result(success=success, data=data, status_code=status_code,
                          error=error, raw=raw)
        except Exception as e:
            return Result(success=False, error=str(e))

    def call(
        self,
        definition: dict,
        endpoint_name: str,
        params: dict,
        raise_on_error: bool = False,
    ) -> Result:
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
        for pname, pdef in endpoint.get("params", {}).items():
            if pdef.get("required") and pname not in params:
                result = Result(
                    success=False,
                    error=f"Required parameter '{pname}' missing for "
                          f"'{definition['name']}.{endpoint_name}'",
                )
                if raise_on_error:
                    result.raise_for_error()
                return result

        retry_cfg = self._retry_config(definition, endpoint)
        max_attempts = max(1, retry_cfg.get("max_attempts", 1))
        backoff_base_ms = retry_cfg.get("backoff_base_ms", 200)
        retry_on = set(retry_cfg.get("retry_on", []))
        auth_type = definition.get("auth", {}).get("type", "none")

        result = Result(success=False, error="No attempts made")
        for attempt in range(max_attempts):
            if attempt > 0:
                wait_s = (backoff_base_ms * (2 ** (attempt - 1))) / 1000.0
                time.sleep(wait_s)

            result = self._execute(definition, endpoint, endpoint_name, params)

            # 401 with OAuth2 → invalidate + retry once
            if result.status_code == 401 and auth_type == "oauth2" and attempt == 0:
                result = self._execute(
                    definition, endpoint, endpoint_name, params, force_refresh=True
                )
                if result.success:
                    break

            if result.success:
                break
            if result.status_code not in retry_on:
                break

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
            r = self.call(definition, endpoint_name, params, raise_on_error=False)
            callback(r)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t
