"""Single-parent API key configuration and request ancestry."""
from copy import deepcopy
import re
from uuid import NAMESPACE_URL, uuid5

IDENTITY_FIELDS = {"api", "id", "parent_id", "name", "role", "enabled"}
PRIVATE_PREFERENCES = {"created_at", "name", "group"}


def raw_key_config(key: dict) -> dict:
    """Return only explicitly configured values, never materialize inheritance."""
    raw = key.get("_raw_config", key)
    return deepcopy({k: v for k, v in raw.items() if not k.startswith("_")})


def prepare_key_hierarchy(keys: list) -> list:
    """Validate and resolve a forest iteratively (no recursion depth ceiling)."""
    raw_keys = [raw_key_config(key) for key in keys]
    by_id = {}
    apis = set()
    for key in raw_keys:
        # Stable migration even when read-only config loading cannot write IDs back.
        key.setdefault("id", str(uuid5(NAMESPACE_URL, "zoaholic:api-key:" + str(key.get("api", "")).strip())))
        if (not isinstance(key["id"], str)
                or re.fullmatch(r"[A-Za-z0-9_-]{1,191}", key["id"]) is None
                or key["id"] in by_id):
            raise ValueError("API key id must be unique and contain 1–191 letters, digits, underscores or hyphens")
        api = str(key.get("api", "")).strip()
        if not api or api in apis:
            raise ValueError("API key credentials must be nonempty and unique")
        key["api"] = api
        apis.add(api)
        by_id[key["id"]] = key
    resolved = {}
    for key in raw_keys:
        path, visiting = [], set()
        cursor = key
        while cursor["id"] not in resolved:
            identity = cursor["id"]
            if identity in visiting:
                raise ValueError("API key parent relationship contains a cycle")
            visiting.add(identity)
            path.append(cursor)
            parent_id = cursor.get("parent_id")
            if parent_id is None:
                break
            if not isinstance(parent_id, str) or parent_id not in by_id:
                raise ValueError("API key parent_id does not reference an existing key")
            cursor = by_id[parent_id]
        for raw in reversed(path):
            parent = resolved.get(raw.get("parent_id"))
            effective = {}
            if parent:
                effective = deepcopy({k: v for k, v in parent.items()
                                      if k not in IDENTITY_FIELDS and not k.startswith("_")})
                effective["preferences"] = {
                    k: v for k, v in (effective.get("preferences") or {}).items()
                    if k not in PRIVATE_PREFERENCES
                }
            for field, value in raw.items():
                if field == "preferences" and isinstance(value, dict):
                    effective[field] = {**(effective.get(field) or {}), **deepcopy(value)}
                else:
                    effective[field] = deepcopy(value)
            effective["_raw_config"] = deepcopy(raw)
            effective["_ancestor_ids"] = [*parent["_ancestor_ids"], parent["id"]] if parent else []
            effective["_ancestor_keys"] = [*parent["_ancestor_keys"], parent["api"]] if parent else []
            resolved[raw["id"]] = effective
    return [resolved[key["id"]] for key in raw_keys]


def key_chain(config: dict, api: str) -> list:
    """Current key first, then nearest parent through root."""
    keys = config.get("api_keys", [])
    cached = config.get("_key_hierarchy_index")
    if cached is None or cached[0] is not keys:
        cached = (keys, {key["api"]: key for key in keys})
        config["_key_hierarchy_index"] = cached
    by_api = cached[1]
    current = by_api.get(api)
    if current is None:
        return []
    return [current, *(by_api[parent] for parent in reversed(current.get("_ancestor_keys", [])))]


def snapshot_key_ancestry(info: dict, key: dict) -> None:
    """Freeze ancestry once per request; subsequent config edits cannot move usage."""
    if "key_ancestor_keys" not in info:
        info["api_key_id"] = key.get("id")
        info["key_ancestor_ids"] = list(key.get("_ancestor_ids", []))
        info["key_ancestor_keys"] = list(key.get("_ancestor_keys", []))
