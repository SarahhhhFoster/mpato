"""
Resolves credential values from environment variables, files, or static config.
"""

import os
from pathlib import Path
from typing import Optional

from mpato.loader import DefinitionError


class ResolverError(Exception):
    pass


def resolve_credential(resolve_config: dict, service_name: str) -> Optional[str]:
    """
    Resolve a credential value using the given resolve config.

    Strategies:
      env    - read from environment variable named by 'key'
      file   - read first line of file at 'path'
      static - use 'value' directly
    """
    if resolve_config is None:
        return None

    strategy = resolve_config.get("strategy", "").lower()

    if strategy == "env":
        key = resolve_config.get("key")
        if not key:
            raise ResolverError(
                f"auth.resolve.key is required for env strategy in '{service_name}'"
            )
        value = os.environ.get(key)
        if value is None:
            raise ResolverError(
                f"Environment variable '{key}' not set (required for '{service_name}' auth)"
            )
        return value

    elif strategy == "file":
        path = resolve_config.get("path")
        if not path:
            raise ResolverError(
                f"auth.resolve.path is required for file strategy in '{service_name}'"
            )
        fpath = Path(path)
        if not fpath.exists():
            raise ResolverError(
                f"Credential file '{path}' not found (required for '{service_name}' auth)"
            )
        return fpath.read_text(encoding="utf-8").splitlines()[0].strip()

    elif strategy == "static":
        value = resolve_config.get("value")
        if value is None:
            raise ResolverError(
                f"auth.resolve.value is required for static strategy in '{service_name}'"
            )
        return str(value)

    else:
        raise ResolverError(
            f"Unknown resolve strategy '{strategy}' in '{service_name}'"
        )
