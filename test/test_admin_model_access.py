from types import SimpleNamespace

import pytest

from core.model_catalog import post_all_models
from core.routing import get_matching_providers


def _provider(name, model, group, enabled=True):
    return {
        "provider": name,
        "model": [model],
        "groups": [group],
        "enabled": enabled,
        "_model_dict_cache": {model: model},
    }


def _config(role="admin"):
    return {
        "api_keys": [{
            "api": "hidden",
            "role": role,
            "model": ["all"],
            "groups": ["tier-a"],
            "preferences": {
                "excluded_channels": ["tier-b-provider"],
                "excluded_models": ["model-b"],
            },
        }],
        "providers": [
            _provider("tier-a-provider", "model-a", "tier-a"),
            _provider("tier-b-provider", "model-b", "tier-b"),
            _provider("disabled-provider", "model-disabled", "tier-b", enabled=False),
        ],
        "preferences": {},
    }


class _App:
    state = SimpleNamespace(api_list=[], models_list={})


def test_admin_model_catalog_lists_all_enabled_groups():
    ids = {item["id"] for item in post_all_models(0, _config(), [], {})}
    assert ids == {"model-a", "model-b"}


@pytest.mark.asyncio
async def test_admin_routing_bypasses_groups_and_user_exclusions():
    matched = await get_matching_providers("model-b", _config(), 0, _App())
    assert [item["provider"] for item in matched] == ["tier-b-provider"]


def test_ordinary_model_catalog_keeps_group_isolation():
    ids = {item["id"] for item in post_all_models(0, _config(role="user"), [], {})}
    assert ids == {"model-a"}


@pytest.mark.asyncio
async def test_ordinary_routing_keeps_groups_and_exclusions():
    assert await get_matching_providers("model-b", _config(role="user"), 0, _App()) == []
