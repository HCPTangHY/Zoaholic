"""Exercise hierarchy through configuration, authentication and request routing."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from core.auth import verify_api_key
from core.config.serialization import _sanitize_config_for_persistence
from core.config.service import update_config
from core.middleware import request_info
from core.routing import get_matching_providers
from routes.models import build_models_for_request


def configuration():
    return {
        'providers': [{'provider': 'upstream', 'base_url': 'https://example.invalid/v1',
                       'api': 'upstream-test-key', 'model': ['allowed', 'blocked'], 'groups': ['shared']}],
        'api_keys': [
            {'id': 'root', 'api': 'root-test-key', 'model': ['allowed'], 'groups': ['shared'],
             'preferences': {'timeout': 40}},
            {'id': 'middle', 'api': 'middle-test-key', 'parent_id': 'root'},
            {'id': 'leaf', 'api': 'leaf-test-key', 'parent_id': 'middle',
             'model': ['all'], 'preferences': {'timeout': 0}},
        ], 'preferences': {},
    }


async def load(raw):
    config, keys, api_list = await update_config(deepcopy(raw), save_to_file=False,
                                               save_to_db=False, skip_model_fetch=True)
    return SimpleNamespace(state=SimpleNamespace(config=config, api_keys_db=keys,
                                                api_list=api_list, models_list={}))


@pytest.mark.asyncio
async def test_save_reload_does_not_freeze_inherited_fields():
    app = await load(configuration())
    saved = _sanitize_config_for_persistence(app.state.config)
    assert 'model' not in saved['api_keys'][1]
    assert 'preferences' not in saved['api_keys'][1]
    saved['api_keys'][0]['model'] = ['blocked']
    saved['api_keys'][0]['preferences']['timeout'] = 55
    second = await load(saved)
    assert second.state.api_keys_db[1]['model'] == ['blocked']
    assert second.state.api_keys_db[1]['preferences']['timeout'] == 55
    assert second.state.api_keys_db[2]['preferences']['timeout'] == 0
    assert [key['id'] for key in second.state.api_keys_db] == ['root', 'middle', 'leaf']


@pytest.mark.asyncio
async def test_effective_models_agree_with_routing_for_three_levels():
    app = await load(configuration())
    visible = await build_models_for_request(2, app)
    assert [item['id'] for item in visible] == ['allowed']
    assert await get_matching_providers('allowed', app.state.config, 2, app)
    assert not await get_matching_providers('blocked', app.state.config, 2, app)


@pytest.mark.asyncio
async def test_explicit_empty_models_deny_instead_of_defaulting_to_all():
    raw = configuration()
    raw['api_keys'][1]['model'] = []
    app = await load(raw)
    assert app.state.api_keys_db[1]['model'] == []
    assert not await get_matching_providers('allowed', app.state.config, 2, app)
    assert await build_models_for_request(2, app) == []


@pytest.mark.asyncio
async def test_http_auth_keeps_leaf_identity_and_request_time_ancestry():
    app = await load(configuration())
    request = Request({'type': 'http', 'app': app, 'headers': [(b'x-api-key', b'leaf-test-key')]})
    context = request_info.set({'client_ip': '127.0.0.1'})
    try:
        assert await verify_api_key(request, None) == 2
        info = request_info.get()
        assert info['api_key'] == 'leaf-test-key'
        assert info['api_key_id'] == 'leaf'
        assert info['key_ancestor_ids'] == ['root', 'middle']
        assert info['key_ancestor_keys'] == ['root-test-key', 'middle-test-key']
    finally:
        request_info.reset(context)


@pytest.mark.asyncio
async def test_disabled_ancestor_blocks_leaf_but_not_unrelated_key():
    raw = configuration()
    raw['api_keys'][1]['enabled'] = False
    app = await load(raw)
    request = Request({'type': 'http', 'app': app, 'headers': [(b'x-api-key', b'leaf-test-key')]})
    context = request_info.set({'client_ip': '127.0.0.1'})
    try:
        with pytest.raises(HTTPException) as error:
            await verify_api_key(request, None)
        assert error.value.status_code == 403
        root_request = Request({'type': 'http', 'app': app, 'headers': [(b'x-api-key', b'root-test-key')]})
        assert await verify_api_key(root_request, None) == 0
    finally:
        request_info.reset(context)
