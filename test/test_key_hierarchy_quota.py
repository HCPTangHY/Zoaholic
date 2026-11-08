import json
import sqlite3
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select

from core.quota.runtime import QuotaRegistry


@pytest.fixture
def registry(monkeypatch):
    # No snapshot writes or dependence on a deployment's saved counters.
    monkeypatch.setattr(QuotaRegistry, '_save_snapshot', lambda self: None)
    monkeypatch.setattr(QuotaRegistry, '_read_snapshot_file', lambda self: {})
    return QuotaRegistry()


def key(identity, parent=None, limit='100/inf'):
    result = {'id': identity, 'api': 'key-' + identity,
              'preferences': {'quota': {'token': limit, 'request': '100/min:fixed'}}}
    if parent:
        result['parent_id'] = parent
    return result


def current(registry, api, metric='token'):
    return registry.get_key_status(api)['key:' + metric + ':default']['current']


def test_parent_child_and_global_accounting(registry):
    registry.init_from_config({'preferences': {'quota': {'token': '1000/inf', 'request': '100/min:fixed'}},
                              'api_keys': [key('root'), key('a', 'root', '10/inf'), key('b', 'root')]})
    assert registry.check('key-a') is None
    registry.record_usage('key-a', 'm', tokens=10)
    assert current(registry, 'key-root') == 10
    assert registry._global.get_status()['key:token:default']['current'] == 10
    assert registry._global.get_status()['key:request:default']['current'] == 1
    assert registry.is_exhausted('key-a')
    assert not registry.is_exhausted('key-b')
    assert registry.check('key-a')
    assert current(registry, 'key-root', 'request') == 1
    assert registry.check('key-b') is None
    registry.record_usage('key-b', 'm', tokens=90)
    assert registry.is_exhausted('key-root')
    assert registry.is_exhausted('key-b')


def test_unconfigured_child_still_checks_ancestors(registry):
    registry.init_from_config({'api_keys': [key('root', limit='1/inf'),
                                          {'id': 'a', 'api': 'key-a', 'parent_id': 'root'}]})
    assert registry.has_quota('key-a')
    registry.record_usage('key-a', 'm', tokens=1)
    assert registry.is_exhausted('key-a')


def test_multilevel_migration_and_rotation_keep_request_chain(registry):
    config = {'api_keys': [key('root'), key('mid', 'root'), key('child', 'mid'), key('other')]}
    registry.init_from_config(config)
    registry.record_usage('key-child', 'm', tokens=3, ancestor_keys=['key-root', 'key-mid'])
    moved = deepcopy(config)
    moved['api_keys'][2]['parent_id'] = 'other'
    moved['api_keys'][0]['api'] = 'root-rotated'
    registry.init_from_config(moved)
    assert current(registry, 'root-rotated') == 3
    registry.record_usage('key-child', 'm', tokens=4, ancestor_keys=['key-root', 'key-mid'])
    assert current(registry, 'root-rotated') == 7
    assert current(registry, 'key-mid') == 7
    assert current(registry, 'key-other') == 0
    registry.record_usage('key-child', 'm', tokens=2, ancestor_keys=[])
    assert current(registry, 'key-other') == 0
    registry.record_usage('key-child', 'm', tokens=1)
    assert current(registry, 'key-other') == 1


def test_failed_dimensional_parent_does_not_charge_global_or_child(registry):
    root = key('root')
    root['preferences']['quota_rules'] = [{'id': 'once', 'measure': 'request',
                                          'aggregate': 'count', 'group_by': [], 'limit': '1/day:fixed'}]
    registry.init_from_config({'preferences': {'quota': {'request': '10/min:fixed'}},
                              'api_keys': [root, key('child', 'root')]})
    assert registry.check('key-child', 'm') is None
    assert registry.check('key-child', 'm')
    assert current(registry, 'key-child', 'request') == 1
    assert current(registry, 'key-root', 'request') == 1
    assert registry._global.get_status()['key:request:default']['current'] == 1


def test_history_filters_use_saved_ancestry_not_current_parent():
    from core.stats import key_history_sql, key_history_filter
    from db import RequestStat

    rows = [
        {'api_key_id': 'child', 'api_key': 'key-child', 'key_ancestor_ids': json.dumps(['old'], separators=(',', ':'))},
        {'api_key_id': 'child', 'api_key': 'key-child', 'key_ancestor_ids': json.dumps(['new'], separators=(',', ':'))},
        {'api_key_id': 'old', 'api_key': 'old-rotated', 'key_ancestor_ids': '[]'},
        {'api_key_id': None, 'api_key': 'key-old', 'key_ancestor_ids': None},
        {'api_key_id': 'strange', 'api_key': 'strange', 'key_ancestor_ids': json.dumps(['prefix-old', 'quote"old'])},
    ]
    engine = create_engine('sqlite://')
    RequestStat.__table__.create(engine)
    with engine.begin() as conn:
        conn.execute(RequestStat.__table__.insert(), rows)
        for branch, expected in [(False, 2), (True, 3)]:
            result = conn.execute(select(RequestStat.id).where(key_history_filter('old', 'key-old', branch))).all()
            assert len(result) == expected
    # D1 uses the same SQLite-compatible clause and parameter binding.
    conn = sqlite3.connect(':memory:')
    conn.execute('create table request_stats(api_key_id text, api_key text, key_ancestor_ids text)')
    conn.executemany('insert into request_stats values (?, ?, ?)', [tuple(row.values()) for row in rows])
    clause, params = key_history_sql('old', 'key-old', True)
    assert conn.execute('select count(*) from request_stats where ' + clause, params).fetchone()[0] == 3
    conn.close()


def test_counter_snapshot_restart_keeps_stable_identity(monkeypatch, registry):
    registry.init_from_config({'api_keys': [key('root'), key('child', 'root')]})
    registry.record_usage('key-child', 'm', tokens=9)
    snapshot = registry._capture_snapshot()
    monkeypatch.setattr(QuotaRegistry, '_read_snapshot_file', lambda self: snapshot)
    restarted = QuotaRegistry()
    root = key('root')
    root['api'] = 'rotated'
    restarted.init_from_config({'api_keys': [root, key('child', 'root')]})
    assert current(restarted, 'rotated') == 9
    assert current(restarted, 'key-child') == 9


def test_request_identity_survives_rotation_and_credential_reuse(registry):
    registry.init_from_config({'api_keys': [key('a'), key('root')]})
    registry.record_usage('key-a', 'm', tokens=2, api_key_id='a', ancestor_ids=[])
    rotated = key('a')
    rotated['api'] = 'rotated-a'
    recycled = key('b')
    recycled['api'] = 'key-a'
    registry.init_from_config({'api_keys': [rotated, recycled, key('root')]})
    registry.record_usage('key-a', 'm', tokens=3, api_key_id='a', ancestor_ids=[], ancestor_keys=[])
    assert current(registry, 'rotated-a') == 5
    assert current(registry, 'key-a') == 0
    # A deleted identity must never charge a newly created holder of its old credential.
    registry.init_from_config({'api_keys': [recycled, key('root')]})
    registry.record_usage('key-a', 'm', tokens=7, api_key_id='a', ancestor_ids=['root'])
    assert current(registry, 'key-a') == 0
    assert current(registry, 'key-root') == 7


def test_reused_credential_does_not_inherit_deleted_identity_usage(registry):
    registry.init_from_config({'api_keys': [key('a')]})
    registry.record_usage('key-a', 'm', tokens=9)
    replacement = key('b')
    replacement['api'] = 'key-a'
    registry.init_from_config({'api_keys': [replacement]})
    assert current(registry, 'key-a') == 0
