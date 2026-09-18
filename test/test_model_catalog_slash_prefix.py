import asyncio
from types import SimpleNamespace

import pytest

from core.model_catalog import post_all_models
from core.routing import get_provider_rules


def _provider(name="upstream", prefix="vendor/"):
    return {
        "provider": name,
        "model": ["model-a"],
        "model_prefix": prefix,
        "groups": ["default"],
        "_model_dict_cache": {f"{prefix}model-a": "model-a"},
    }


def _config(rule, provider=None):
    return {
        "api_keys": [{
            "api": "zk-test",
            "model": [rule],
            "groups": ["default"],
        }],
        "providers": [provider or _provider()],
        "preferences": {},
    }


def _listed_model_ids(rule, provider=None):
    config = _config(rule, provider)
    return [
        model["id"]
        for model in post_all_models(0, config, [], {})
    ]


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        ("vendor/model-a", "vendor/model-a"),
        ("<vendor/model-a>", "vendor/model-a"),
        ("upstream/vendor/model-a", "vendor/model-a"),
        ("upstream/*", "vendor/model-a"),
    ],
)
def test_slash_prefixed_api_key_model_is_listed(rule, expected):
    assert _listed_model_ids(rule) == [expected]


def test_provider_name_can_also_be_the_model_namespace():
    provider = _provider(name="vendor")
    assert _listed_model_ids("vendor/model-a", provider) == ["vendor/model-a"]


@pytest.mark.parametrize(
    ("rule", "provider", "expected"),
    [
        ("vendor/model-a", _provider(), ["upstream/vendor/model-a"]),
        ("<vendor/model-a>", _provider(), ["upstream/vendor/model-a"]),
        ("upstream/vendor/model-a", _provider(), ["upstream/vendor/model-a"]),
        ("upstream/*", _provider(), ["upstream/vendor/model-a"]),
        ("vendor/model-a", _provider(name="vendor"), ["vendor/vendor/model-a"]),
    ],
)
def test_slash_prefixed_api_key_rule_routes_to_exposing_provider(rule, provider, expected):
    config = _config(rule, provider)
    app = SimpleNamespace(state=SimpleNamespace(api_list=[], models_list={}))

    rules = asyncio.run(get_provider_rules(rule, config, "vendor/model-a", app))

    assert rules == expected
