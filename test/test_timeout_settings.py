from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core.timeout_settings import get_timeout_defaults, validate_timeout_preferences
from routes import admin


def test_environment_defaults_match_startup_integer_parsing(monkeypatch):
    for name in ("TIMEOUT", "STREAM_HEADER_TIMEOUT", "KEEPALIVE_INTERVAL"):
        monkeypatch.delenv(name, raising=False)
    assert get_timeout_defaults() == {
        "model_timeout": 600, "stream_header_timeout": 60, "keepalive_interval": 15,
    }
    monkeypatch.setenv("TIMEOUT", "900")
    monkeypatch.setenv("STREAM_HEADER_TIMEOUT", "300")
    monkeypatch.setenv("KEEPALIVE_INTERVAL", "20")
    assert get_timeout_defaults() == {
        "model_timeout": 900, "stream_header_timeout": 300, "keepalive_interval": 20,
    }


@pytest.mark.parametrize("preferences", [
    {},
    {"model_timeout": 900, "stream_header_timeout": 0, "keepalive_interval": 20},
    {"stream_header_timeout": {"default": 60, "gemini-4-carbon": 0}},
    {"model_timeout": {"default": 600, "gemini-4-carbon": 900.5}},
    {"header_timeout": 0.25, "keepalive_interval": {"default": 0, "other": 20}},
    {"header_timeout": -1, "stream_header_timeout": -0.5, "keepalive_interval": -1},
    {"model_timeout": None, "stream_header_timeout": None, "header_timeout": None, "keepalive_interval": None},
    {"stream_header_timeout": {}, "plugin_timeout": "not part of this contract"},
])
def test_supported_values_are_not_rewritten(preferences):
    before = deepcopy(preferences)
    validate_timeout_preferences(preferences)
    assert preferences == before


@pytest.mark.parametrize("field,value", [
    ("model_timeout", 0), ("model_timeout", -1),
    ("stream_header_timeout", True), ("header_timeout", "60"),
    ("keepalive_interval", 0.5), ("keepalive_interval", -0.5),
    ("model_timeout", float("nan")), ("stream_header_timeout", float("inf")),
    ("model_timeout", 10 ** 400),
    ("header_timeout", float("-inf")), ("stream_header_timeout", []),
    ("stream_header_timeout", {"": 60}), ("header_timeout", {"  ": 60}),
    ("keepalive_interval", {3: 20}),
    ("stream_header_timeout", {"gemini": 60, "GEMINI": 0}),
    ("stream_header_timeout", {"gemini": 60, " gemini ": 0}),
    ("model_timeout", {"default": 0}), ("header_timeout", {"model": None}),
])
def test_invalid_values_are_rejected(field, value):
    with pytest.raises(ValueError):
        validate_timeout_preferences({field: value})


@pytest.mark.parametrize("preferences,expected", [
    ({}, 60),
    ({"stream_header_timeout": None}, 60),
    ({"stream_header_timeout": None, "header_timeout": 90}, 90),
    ({"stream_header_timeout": 0, "header_timeout": 90}, 0),
    ({"stream_header_timeout": 0.25, "header_timeout": 90}, 0.25),
    ({"header_timeout": {"default": 60, "gemini-4-carbon": 0}}, 0),
])
def test_alias_precedence_matches_runtime(preferences, expected):
    from main import init_preference
    from core.handler import get_preference
    validate_timeout_preferences(preferences)
    config = {"preferences": {}, "providers": [{"provider": "channel", "preferences": preferences}]}
    table = init_preference(config, "stream_header_timeout", 60, alias_keys=("header_timeout",))
    assert get_preference(table, "channel", ("gemini-4-carbon", "gemini-4-carbon"), 60) == expected


def test_fractional_model_timeout_survives_runtime_lookup():
    from main import init_preference
    from core.handler import get_preference
    preferences = {"model_timeout": {"default": 600, "gemini-4-carbon": 900.5}}
    validate_timeout_preferences(preferences)
    table = init_preference({"preferences": preferences, "providers": [{"provider": "channel"}]}, "model_timeout", 600)
    assert get_preference(table, "channel", ("gemini-4-carbon", "gemini-4-carbon"), 600) == 900.5


@pytest.fixture
def app(monkeypatch):
    instance = SimpleNamespace(state=SimpleNamespace(config={
        "providers": [{"provider": "channel", "preferences": {"header_timeout": 300}}],
        "api_keys": [{"id": "admin", "api": "test-admin-key"}],
        "preferences": {"stream_header_timeout": 60},
    }))
    monkeypatch.setattr(admin, "get_app", lambda: instance)
    monkeypatch.setattr(admin, "_persist_config", AsyncMock(return_value={}))
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"preferences": {"stream_header_timeout": True}},
    {"preferences": {"model_timeout": {"default": 0}}},
    {"providers": [{"provider": "channel", "preferences": {"header_timeout": float("inf")}}]},
    {"providers": [{"provider": "channel", "preferences": {"keepalive_interval": 2.5}}]},
    {"providers": [{"provider": "channel", "sub_channels": [{"engine": "gemini", "preferences": {"header_timeout": True}}]}]},
])
async def test_invalid_admin_update_never_persists_or_changes_live_config(app, payload):
    before = deepcopy(app.state.config)
    with pytest.raises(HTTPException) as error:
        await admin.api_config_update(0, payload)
    assert error.value.status_code == 400
    admin._persist_config.assert_not_awaited()
    assert app.state.config == before


@pytest.mark.asyncio
async def test_global_and_channel_zero_and_alias_survive_save(app):
    payload = {
        "preferences": {"stream_header_timeout": {"default": 60, "gemini-4-carbon": 0}, "keepalive_interval": 0},
        "providers": [{"provider": "channel", "preferences": {"header_timeout": {"default": 300, "gemini-4-carbon": 0}}}],
    }
    await admin.api_config_update(0, payload)
    candidate = admin._persist_config.await_args.kwargs["candidate_config"]
    assert candidate["preferences"] == payload["preferences"]
    assert candidate["providers"] == payload["providers"]


@pytest.mark.asyncio
async def test_null_deletes_global_timeout_without_losing_unrelated_settings(app):
    app.state.config["preferences"]["unrelated"] = "keep"
    await admin.api_config_update(0, {"preferences": {"stream_header_timeout": None}})
    candidate = admin._persist_config.await_args.kwargs["candidate_config"]
    assert candidate["preferences"] == {"unrelated": "keep"}


@pytest.mark.asyncio
async def test_metadata_is_separate_from_persistable_config(app, monkeypatch):
    monkeypatch.setenv("TIMEOUT", "900")
    monkeypatch.setenv("STREAM_HEADER_TIMEOUT", "120")
    monkeypatch.setenv("KEEPALIVE_INTERVAL", "20")
    before = deepcopy(app.state.config)
    response = await admin.api_config(0)
    result = json.loads(response.body)
    assert result["timeout_defaults"] == {"model_timeout": 900, "stream_header_timeout": 120, "keepalive_interval": 20}
    assert "timeout_defaults" not in result["api_config"]
    assert app.state.config == before
