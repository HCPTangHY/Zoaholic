"""Shared API-key role checks used by catalog and request routing."""

from typing import Any, Mapping


def is_admin_api_key(config: Mapping[str, Any], api_index: int) -> bool:
    """Return whether the configured API key at *api_index* has an admin role."""
    try:
        key = (config.get("api_keys") or [])[api_index]
    except (IndexError, TypeError):
        return False
    return isinstance(key, Mapping) and "admin" in str(key.get("role") or "").lower()
