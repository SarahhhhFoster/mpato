"""
MPATO Service Discovery

Loads service definitions from a flat static-file registry described by an
mpato-index.json index file. The index and its definition files can live on
disk (relative or absolute paths) or be served from any HTTP/HTTPS URL —
including a CDN or a raw GitHub URL.

Index format (mpato-index.json):

    {
      "mpato_index_version": "1",
      "name": "...",
      "description": "...",
      "services": [
        {
          "name":        "github",
          "description": "GitHub REST API",
          "version":     "1.0.0",
          "auth_type":   "bearer",
          "tags":        ["vcs", "code"],
          "definition":  "github.yaml"      // relative path OR absolute URL
        },
        ...
      ]
    }

The "definition" field in each entry may be:
  - A relative path   → resolved relative to the index file's location
  - An absolute path  → used as-is
  - An http/https URL → fetched over the network

Usage:

    from mpato.discovery import discover

    # From a local index file
    services = discover("registry/mpato-index.json")

    # From a remote index (fetches index + all definitions)
    services = discover("https://example.com/registry/mpato-index.json")

    # Load discovered services into a registry
    from mpato import ServiceRegistry
    registry = ServiceRegistry()
    for svc in services:
        registry.load(svc.local_path)

    # Or use the convenience method on ServiceRegistry:
    registry.load_index("registry/mpato-index.json")

    # Filter by tag before loading:
    for svc in discover("registry/mpato-index.json"):
        if "free" in svc.tags:
            registry.load(svc.local_path)
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urljoin, urlparse

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


SUPPORTED_INDEX_VERSIONS = {"1"}


class DiscoveryError(Exception):
    """Raised when an index file cannot be loaded or is invalid."""


@dataclass
class ServiceEntry:
    """A single entry from an mpato-index.json, with its definition available locally."""
    name: str
    description: str
    version: str
    auth_type: str
    tags: List[str]
    definition_ref: str    # original value from the index (path or URL)
    local_path: str        # resolved local filesystem path to the definition file
    _tempfile: bool = field(default=False, repr=False)  # True if we fetched to a tempfile

    def __del__(self):
        # Clean up tempfiles we created during remote discovery
        if self._tempfile:
            try:
                os.unlink(self.local_path)
            except Exception:
                pass


def _is_url(ref: str) -> bool:
    return ref.startswith("http://") or ref.startswith("https://")


def _fetch_text(url: str) -> str:
    """Fetch text content from a URL."""
    if not HAS_REQUESTS:
        raise DiscoveryError(
            "requests library is required for remote discovery. "
            "Install with: pip install requests"
        )
    try:
        resp = _requests.get(url, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        raise DiscoveryError(f"Failed to fetch '{url}': {e}") from e


def _resolve_definition(definition_ref: str, index_base: str) -> tuple[str, bool]:
    """
    Resolve a definition reference to a local file path.

    Returns (local_path, is_tempfile).
    - For local refs: resolves relative to index_base directory.
    - For URL refs: fetches content to a named tempfile.
    """
    if _is_url(definition_ref):
        content = _fetch_text(definition_ref)
        suffix = ".yaml" if definition_ref.endswith((".yaml", ".yml")) else ".json"
        tf = tempfile.NamedTemporaryFile(
            suffix=suffix, mode="w", delete=False, encoding="utf-8"
        )
        tf.write(content)
        tf.close()
        return tf.name, True

    # Local path — resolve relative to index base
    if os.path.isabs(definition_ref):
        return definition_ref, False

    resolved = os.path.normpath(os.path.join(index_base, definition_ref))
    return resolved, False


def _validate_index(index: dict, source: str):
    """Validate required fields in the index. Raises DiscoveryError on problems."""
    version = str(index.get("mpato_index_version", ""))
    if not version:
        raise DiscoveryError(
            f"Index '{source}' is missing required field 'mpato_index_version'"
        )
    if version not in SUPPORTED_INDEX_VERSIONS:
        raise DiscoveryError(
            f"Index '{source}' has unsupported version '{version}'. "
            f"Supported: {SUPPORTED_INDEX_VERSIONS}"
        )
    if "services" not in index:
        raise DiscoveryError(f"Index '{source}' is missing required field 'services'")
    if not isinstance(index["services"], list):
        raise DiscoveryError(f"'services' in index '{source}' must be a list")


def discover(
    index_source: str,
    tags: Optional[List[str]] = None,
    auth_types: Optional[List[str]] = None,
) -> List[ServiceEntry]:
    """
    Load and parse an mpato-index.json, returning a list of ServiceEntry objects.

    Args:
        index_source:  Path or URL to an mpato-index.json file.
        tags:          If provided, only return services whose tags overlap.
        auth_types:    If provided, only return services with matching auth_type.

    Returns:
        List of ServiceEntry objects with .local_path pointing to each
        definition file, ready to pass to ServiceRegistry.load().

    Raises:
        DiscoveryError: If the index cannot be read, is malformed, or a
                        definition file cannot be resolved.
    """
    # ── Load the index ────────────────────────────────────────────────────────
    if _is_url(index_source):
        raw = _fetch_text(index_source)
        # Base for resolving relative definition refs = directory of the index URL
        parsed = urlparse(index_source)
        index_base_url = index_source.rsplit("/", 1)[0] + "/"
        index_base_dir = None
    else:
        fpath = Path(index_source)
        if not fpath.exists():
            raise DiscoveryError(f"Index file not found: '{index_source}'")
        raw = fpath.read_text(encoding="utf-8")
        index_base_dir = str(fpath.parent.resolve())
        index_base_url = None

    try:
        index = json.loads(raw)
    except json.JSONDecodeError as e:
        raise DiscoveryError(f"Failed to parse index '{index_source}': {e}") from e

    _validate_index(index, index_source)

    # ── Parse entries ─────────────────────────────────────────────────────────
    entries = []
    for i, svc in enumerate(index["services"]):
        if not isinstance(svc, dict):
            raise DiscoveryError(
                f"Service entry {i} in '{index_source}' must be a JSON object"
            )

        name = svc.get("name")
        if not name:
            raise DiscoveryError(
                f"Service entry {i} in '{index_source}' is missing 'name'"
            )

        definition_ref = svc.get("definition")
        if not definition_ref:
            raise DiscoveryError(
                f"Service '{name}' in '{index_source}' is missing 'definition'"
            )

        # Resolve relative refs against the index's location
        if _is_url(definition_ref):
            base = ""  # absolute URL, no base needed
        elif index_base_url and not os.path.isabs(definition_ref):
            # Remote index, relative ref → make absolute URL
            definition_ref = urljoin(index_base_url, definition_ref)
            base = ""
        else:
            base = index_base_dir or ""

        local_path, is_temp = _resolve_definition(definition_ref, base)

        entry = ServiceEntry(
            name=name,
            description=svc.get("description", ""),
            version=svc.get("version", "0.0.0"),
            auth_type=svc.get("auth_type", "unknown"),
            tags=svc.get("tags", []),
            definition_ref=definition_ref,
            local_path=local_path,
            _tempfile=is_temp,
        )
        entries.append(entry)

    # ── Filter ────────────────────────────────────────────────────────────────
    if tags:
        tag_set = set(t.lower() for t in tags)
        entries = [e for e in entries if tag_set & {t.lower() for t in e.tags}]

    if auth_types:
        at_set = {a.lower() for a in auth_types}
        entries = [e for e in entries if e.auth_type.lower() in at_set]

    return entries
