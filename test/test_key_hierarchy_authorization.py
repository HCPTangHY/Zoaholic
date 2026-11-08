"""Hierarchy authorization must constrain the selected upstream and credential destination."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.byok import reset_byok_context, set_byok_context
from core.key_hierarchy import prepare_key_hierarchy
from core.routing import get_matching_providers
from core.utils import get_model_dict
from routes.models import build_models_for_request


def app_for(keys, providers):
    keys = prepare_key_hierarchy(keys)
    config = {'api_keys': keys, 'providers': providers, 'preferences': {}}
    return SimpleNamespace(state=SimpleNamespace(config=config, api_keys_db=keys,
                                                api_list=[key['api'] for key in keys], models_list={}))


@pytest.mark.asyncio
async def test_wildcard_request_intersects_resolved_upstream_not_only_provider():
    app = app_for([
        {'id': 'parent', 'api': 'parent-key', 'model': ['p/gpt-4'], 'groups': ['default']},
        {'id': 'child', 'api': 'child-key', 'parent_id': 'parent', 'model': ['all']},
    ], [{'provider': 'p', 'base_url': 'https://example.invalid', 'api': 'upstream',
         'model': ['gpt-4', 'gpt-5'], 'groups': ['default']}])
    parent = await get_matching_providers('gpt-*', app.state.config, 0, app)
    child = await get_matching_providers('gpt-*', app.state.config, 1, app)
    assert [get_model_dict(p)['gpt-*'] for p in parent] == ['gpt-4']
    assert [get_model_dict(p)['gpt-*'] for p in child] == ['gpt-4']
    assert not await get_matching_providers('gpt-5', app.state.config, 1, app)


@pytest.mark.asyncio
async def test_wildcard_aliases_retain_real_upstream_mapping():
    app = app_for([
        {'id': 'parent', 'api': 'parent-key', 'model': ['p/public-a'], 'groups': ['default']},
        {'id': 'child', 'api': 'child-key', 'parent_id': 'parent', 'model': ['all']},
    ], [{'provider': 'p', 'base_url': 'https://example.invalid', 'api': 'upstream',
         'model': [{'upstream-one': 'public-a'}, {'upstream-two': 'public-b'}], 'groups': ['default']}])
    child = await get_matching_providers('public-*', app.state.config, 1, app)
    assert [get_model_dict(p)['public-*'] for p in child] == ['upstream-one']


@pytest.mark.parametrize('restriction', ['model', 'groups', 'empty_groups', 'excluded_channel', 'excluded_models'])
@pytest.mark.asyncio
async def test_byok_discovery_never_sends_credentials_to_ancestor_denied_channel(monkeypatch, restriction):
    root = {'id': 'root', 'api': 'root-key', 'model': ['all'], 'groups': ['a', 'b']}
    if restriction == 'model':
        root['model'] = ['allowed/*']
    elif restriction == 'groups':
        root['groups'] = ['a']
    elif restriction == 'empty_groups':
        root['groups'] = []
    elif restriction == 'excluded_channel':
        root['preferences'] = {'excluded_channels': ['blocked']}
    else:
        root['preferences'] = {'excluded_models': ['blocked/*']}
    app = app_for([root, {'id': 'mid', 'api': 'mid-key', 'parent_id': 'root'},
                   {'id': 'leaf', 'api': 'byok-*', 'parent_id': 'mid', 'model': ['blocked/*'],
                    'groups': ['a', 'b'], 'preferences': {'excluded_channels': []}}], [
        {'provider': 'allowed', 'base_url': 'https://allowed.invalid', 'api': ['*'], 'model': ['*'], 'groups': ['a']},
        {'provider': 'blocked', 'base_url': 'https://blocked.invalid', 'api': ['*'], 'model': ['*'], 'groups': ['b']},
    ])
    fetch = AsyncMock(return_value=['model-x'])
    monkeypatch.setattr('routes.models._fetch_byok_provider_models', fetch)
    context = set_byok_context('sensitive-upstream-key', 'byok-*')
    try:
        assert await build_models_for_request(2, app) == []
    finally:
        reset_byok_context(context)
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_byok_discovery_allowed_chain_still_fetches(monkeypatch):
    provider = {'provider': 'allowed', 'base_url': 'https://allowed.invalid', 'api': ['*'],
                'model': ['*'], 'groups': ['default']}
    app = app_for([
        {'id': 'root', 'api': 'root-key', 'model': ['allowed/*'], 'groups': ['default']},
        {'id': 'leaf', 'api': 'byok-*', 'parent_id': 'root'},
    ], [provider])
    fetch = AsyncMock(return_value=['model-x'])
    monkeypatch.setattr('routes.models._fetch_byok_provider_models', fetch)
    context = set_byok_context('sensitive-upstream-key', 'byok-*')
    try:
        assert [item['id'] for item in await build_models_for_request(1, app)] == ['model-x']
    finally:
        reset_byok_context(context)
    fetch.assert_awaited_once_with(app, provider, 'sensitive-upstream-key')
