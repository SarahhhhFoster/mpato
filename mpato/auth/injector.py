"""
Injects resolved credentials into HTTP requests (headers, query params, body).
"""

from typing import Any, Dict, Optional, Tuple


def inject_credential(
    credential: Optional[str],
    inject_config: dict,
    headers: dict,
    params: dict,
    body: Any,
) -> Tuple[dict, dict, Any]:
    """
    Inject a credential into headers, query params, or body based on inject_config.

    Returns updated (headers, params, body).
    """
    if credential is None:
        return headers, params, body

    strategy = inject_config.get("strategy", "").lower()
    prefix = inject_config.get("prefix", "")

    value = f"{prefix}{credential}"

    if strategy == "header":
        name = inject_config.get("name", "Authorization")
        headers = {**headers, name: value}

    elif strategy == "query":
        name = inject_config.get("name", "api_key")
        params = {**params, name: credential}  # No prefix for query params

    elif strategy == "body":
        name = inject_config.get("name", "api_key")
        if isinstance(body, dict):
            body = {**body, name: credential}
        else:
            body = {name: credential}

    elif strategy:
        raise ValueError(
            f"Unknown inject strategy '{strategy}'. "
            f"Must be one of: header, query, body"
        )

    return headers, params, body
