"""Timeout defaults and validation at the configuration API boundary."""

import math
import os


TIMEOUT_FIELDS = ("model_timeout", "stream_header_timeout", "header_timeout", "keepalive_interval")


def get_timeout_defaults() -> dict[str, int]:
    """Use the same environment variables and integer parsing as server startup."""
    return {
        "model_timeout": int(os.getenv("TIMEOUT", 600)),
        "stream_header_timeout": int(os.getenv("STREAM_HEADER_TIMEOUT", 60)),
        "keepalive_interval": int(os.getenv("KEEPALIVE_INTERVAL", 15)),
    }


def _validate_seconds(field: str, value, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number of seconds")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{path} must be a finite number of seconds")
    if field == "model_timeout" and value <= 0:
        raise ValueError(f"{path} must be greater than zero")
    # Existing runtime treats nonpositive header/heartbeat values as disabled.
    # Preserve negative legacy settings; the editor writes zero for new disables.
    if field == "keepalive_interval" and value % 1 != 0:
        raise ValueError(f"{path} must be a whole number of seconds")


def validate_timeout_preferences(preferences: dict, owner: str = "preferences") -> None:
    """Validate without rewriting scalar, model-map, alias or explicit zero values.

    A missing or null top-level field falls back to inherited/server settings.
    Null inside a model map is not an explicit timeout and is rejected.
    Negative header/heartbeat values retain their existing disabled semantics.
    """
    for field in TIMEOUT_FIELDS:
        value = preferences.get(field)
        if value is None:
            continue
        path = f"{owner}.{field}"
        if not isinstance(value, dict):
            _validate_seconds(field, value, path)
            continue
        seen = set()
        for model, seconds in value.items():
            if not isinstance(model, str) or not model.strip():
                raise ValueError(f"{path} model names must be nonempty strings")
            normalized = model.strip().casefold()
            if normalized in seen:
                raise ValueError(f"{path} contains duplicate model names (case-insensitive)")
            seen.add(normalized)
            _validate_seconds(field, seconds, f"{path}[{model!r}]")
