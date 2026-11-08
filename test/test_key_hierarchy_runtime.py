from types import SimpleNamespace

import pytest
from fastapi import WebSocket

from core.key_hierarchy import prepare_key_hierarchy, raw_key_config


@pytest.mark.asyncio
async def test_config_roundtrip_preserves_overrides_and_inheritance():
    from core.config.service import update_config
    config = {'providers': [], 'api_keys': [
        {'api': 'root', 'id': 'r', 'model': [], 'groups': [], 'preferences': {'cooldown_period': 0}},
        {'api': 'child', 'parent_id': 'r', 'preferences': {'enabled': False}},
    ]}
    runtime, keys, _ = await update_config(config, save_to_file=False, skip_model_fetch=True)
    assert keys[0]['model'] == keys[1]['model'] == []
    assert keys[0]['groups'] == keys[1]['groups'] == []
    assert keys[1]['preferences']['cooldown_period'] == 0
    assert keys[1]['preferences']['enabled'] is False
    assert 'model' not in raw_key_config(keys[1])
    second, _, _ = await update_config(runtime, save_to_file=False, skip_model_fetch=True)
    assert raw_key_config(second['api_keys'][1]) == raw_key_config(keys[1])


def test_ws_auth_rechecks_current_key_index_and_parent_disable():
    from core.dialects.openai_responses_ws import _authenticate
    keys = prepare_key_hierarchy([{'api': 'root', 'id': 'r'}, {'api': 'child', 'parent_id': 'r'}])
    app = SimpleNamespace(state=SimpleNamespace(api_keys_db=keys, api_list=['root', 'child']))
    socket = WebSocket({'type': 'websocket', 'app': app,
                        'headers': [(b'authorization', b'Bearer child')], 'client': ('127.0.0.1', 80)}, None, None)
    assert _authenticate(socket) == (1, 'child', None)
    app.state.api_keys_db = list(reversed(keys))
    app.state.api_list = ['child', 'root']
    assert _authenticate(socket) == (0, 'child', None)
    app.state.api_keys_db[1]['enabled'] = False
    assert _authenticate(socket)[0] is None
