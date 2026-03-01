"""
OAuth2 client credentials flow with token caching and refresh.
"""

import time
import threading
from typing import Optional

from mpato.auth.resolver import resolve_credential, ResolverError

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class OAuth2Error(Exception):
    pass


class OAuth2TokenCache:
    """Thread-safe OAuth2 token cache with expiry."""

    def __init__(self):
        self._lock = threading.Lock()
        self._tokens: dict = {}  # keyed by token_url + client_id

    def _key(self, token_url: str, client_id: str) -> str:
        return f"{token_url}:{client_id}"

    def get(self, token_url: str, client_id: str) -> Optional[str]:
        key = self._key(token_url, client_id)
        with self._lock:
            entry = self._tokens.get(key)
            if entry and entry["expires_at"] > time.time() + 30:
                return entry["token"]
        return None

    def set(self, token_url: str, client_id: str, token: str, expires_in: int):
        key = self._key(token_url, client_id)
        with self._lock:
            self._tokens[key] = {
                "token": token,
                "expires_at": time.time() + expires_in,
            }

    def invalidate(self, token_url: str, client_id: str):
        key = self._key(token_url, client_id)
        with self._lock:
            self._tokens.pop(key, None)


_global_cache = OAuth2TokenCache()


def get_oauth2_token(auth_config: dict, service_name: str) -> str:
    """
    Obtain an OAuth2 bearer token using client credentials flow.
    Uses the global token cache; refreshes on expiry.
    """
    if not HAS_REQUESTS:
        raise OAuth2Error(
            "requests library is required for OAuth2 support. "
            "Install it with: pip install requests"
        )

    token_url = auth_config.get("token_url")
    if not token_url:
        raise OAuth2Error(f"auth.token_url is required for OAuth2 in '{service_name}'")

    # Resolve client_id
    client_id_config = auth_config.get("client_id")
    if isinstance(client_id_config, dict):
        client_id = resolve_credential(client_id_config, service_name)
    else:
        client_id = str(client_id_config)

    # Resolve client_secret
    client_secret_config = auth_config.get("client_secret")
    if isinstance(client_secret_config, dict):
        client_secret = resolve_credential(client_secret_config, service_name)
    else:
        client_secret = str(client_secret_config)

    # Check cache
    cached = _global_cache.get(token_url, client_id)
    if cached:
        return cached

    # Exchange credentials for token
    scope = auth_config.get("scope", "")
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        data["scope"] = scope

    try:
        resp = _requests.post(token_url, data=data, timeout=10)
        resp.raise_for_status()
        token_data = resp.json()
    except Exception as e:
        raise OAuth2Error(f"OAuth2 token exchange failed for '{service_name}': {e}") from e

    access_token = token_data.get("access_token")
    if not access_token:
        raise OAuth2Error(
            f"OAuth2 response missing 'access_token' for '{service_name}': {token_data}"
        )

    expires_in = int(token_data.get("expires_in", 3600))
    _global_cache.set(token_url, client_id, access_token, expires_in)

    return access_token
