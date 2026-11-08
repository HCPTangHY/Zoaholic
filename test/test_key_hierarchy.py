from types import SimpleNamespace

import pytest

from core.key_hierarchy import prepare_key_hierarchy, raw_key_config, key_chain, snapshot_key_ancestry
from core.config.serialization import _sanitize_config_for_persistence


def test_inheritance_raw_and_stable_ids():
    raw = [dict(api='root', id='r', model=['x'], role='admin', preferences={'timeout': 3, 'enabled': True}),
           dict(api='child', parent_id='r', preferences={'timeout': 0, 'enabled': False}, groups=[])]
    keys = prepare_key_hierarchy(raw)
    child = keys[1]
    assert child['model'] == ['x']
    assert 'role' not in child
    assert child['preferences'] == {'timeout': 0, 'enabled': False}
    assert child['groups'] == []
    assert child['id'] == prepare_key_hierarchy(raw)[1]['id']
    assert 'model' not in raw_key_config(child)
    assert raw_key_config(child) == _sanitize_config_for_persistence({'api_keys': keys})['api_keys'][1]
    runtime = {'api_keys': keys}
    assert [k['api'] for k in key_chain(runtime, 'child')] == ['child', 'root']
    assert '_key_hierarchy_index' not in _sanitize_config_for_persistence(runtime)
    info = {}
    snapshot_key_ancestry(info, child)
    snapshot_key_ancestry(info, keys[0])
    assert info['key_ancestor_ids'] == ['r']
    assert info['key_ancestor_keys'] == ['root']


@pytest.mark.parametrize('raw', [
    [{'api': 'a', 'id': 'a', 'parent_id': 'a'}],
    [{'api': 'a', 'id': 'a', 'parent_id': 'b'}, {'api': 'b', 'id': 'b', 'parent_id': 'a'}],
    [{'api': 'a', 'id': 'a', 'parent_id': 'missing'}],
    [{'api': 'a', 'id': 'same'}, {'api': 'b', 'id': 'same'}],
    [{'api': 'a', 'id': 'embedded"id'}],
    [{'api': 'a', 'id': 'x' * 192}],
])
def test_invalid_relationships(raw):
    with pytest.raises(ValueError):
        prepare_key_hierarchy(raw)


@pytest.mark.asyncio
async def test_ancestor_model_and_group_constraints():
    from core.routing import get_matching_providers
    keys = prepare_key_hierarchy([
        {'api': 'root', 'id': 'r', 'model': ['p/x'], 'groups': ['a']},
        {'api': 'child', 'parent_id': 'r', 'model': ['all'], 'groups': ['a', 'b']},
    ])
    config = {'api_keys': keys, 'providers': [
        {'provider': 'p', 'api': 'up', 'base_url': 'https://example.invalid', 'model': ['x', 'y'], 'groups': ['a']},
        {'provider': 'q', 'api': 'up', 'base_url': 'https://example.invalid', 'model': ['x', 'y'], 'groups': ['b']},
    ]}
    app = SimpleNamespace(state=SimpleNamespace(api_list=['root', 'child'], api_keys_db=keys))
    assert [p['provider'] for p in await get_matching_providers('x', config, 1, app)] == ['p']
    assert await get_matching_providers('y', config, 1, app) == []


def test_access_checks_ancestor_without_inheriting_role():
    from fastapi import HTTPException
    from core.auth import check_key_ancestry_access
    keys = prepare_key_hierarchy([{'api': 'root', 'id': 'r', 'enabled': False, 'role': 'admin'},
                                  {'api': 'child', 'parent_id': 'r'}])
    app = SimpleNamespace(state=SimpleNamespace(api_keys_db=keys))
    with pytest.raises(HTTPException) as error:
        check_key_ancestry_access(app, 1, '127.0.0.1')
    assert error.value.status_code == 403
