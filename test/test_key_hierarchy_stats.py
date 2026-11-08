import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core import stats


def test_enqueue_settles_without_database_and_uses_request_ancestry(monkeypatch):
    captured = []
    registry = SimpleNamespace(record_usage=lambda *args, **kwargs: captured.append((args, kwargs)))
    app = SimpleNamespace(state=SimpleNamespace(quota_registry=registry))
    monkeypatch.setattr(stats, 'DISABLE_DATABASE', True)
    stats.enqueue_stats({'api_key': 'child', 'model': 'm', 'success': True,
                         'prompt_tokens': 4, 'completion_tokens': 3, 'total_tokens': 7,
                         'key_ancestor_keys': ['old-root']}, app, lambda model: (1, 2))
    assert len(captured) == 1
    assert captured[0][1]['ancestor_keys'] == ['old-root']
    assert captured[0][1]['tokens'] == 7


def test_enqueue_serializes_history_snapshot_without_persisting_parent_secrets(monkeypatch):
    monkeypatch.setattr(stats, 'DISABLE_DATABASE', False)
    monkeypatch.setattr(stats, '_stats_buffer', [])
    monkeypatch.setattr(stats, '_ensure_stats_consumer_started', lambda: None)
    info = {'api_key': 'child', 'api_key_id': 'child-id', 'key_ancestor_ids': ['parent-id'],
            'key_ancestor_keys': ['parent-secret']}
    stats.enqueue_stats(info)
    queued = stats._stats_buffer[0][0]
    info['key_ancestor_ids'].append('later-id')
    info['key_ancestor_keys'].append('later-secret')
    assert json.loads(queued['key_ancestor_ids']) == ['parent-id']
    assert queued['key_ancestor_keys'] == ['parent-secret']
    columns = {column.key for column in stats.RequestStat.__table__.columns}
    assert 'api_key_id' in columns and 'key_ancestor_ids' in columns
    assert 'key_ancestor_keys' not in columns


@pytest.mark.asyncio
async def test_topup_updates_raw_config_and_rebuilds(monkeypatch):
    from routes import stats as route, admin
    created = datetime.now(timezone.utc)
    raw = {'id': 'child', 'api': 'child-key', 'parent_id': 'parent'}
    effective = {**raw, 'preferences': {'credits': 20, 'created_at': created}, '_raw_config': raw}
    state = SimpleNamespace(config={'api_keys': [effective]}, api_list=['child-key'],
                            paid_api_keys_states={'child-key': {'credits': 20, 'created_at': created,
                                                               'total_cost': 0, 'enabled': True}})
    app = SimpleNamespace(state=state)
    monkeypatch.setattr(route, 'get_app', lambda: app)
    saved = []
    async def persist(app, **kwargs):
        assert app.state.config['api_keys'][0] is effective
        app.state.config = kwargs['candidate_config']
        saved.append(app.state.config['api_keys'][0])
    async def refresh(app, api):
        app.state.paid_api_keys_states[api]['credits'] = app.state.config['api_keys'][0]['preferences']['credits']
    monkeypatch.setattr(admin, '_persist_config', persist)
    monkeypatch.setattr(stats, 'update_paid_api_keys_states', refresh)
    response = await route.add_credits_to_api_key(None, paid_key='child-key', amount=5, token='admin')
    assert json.loads(response.body)['new_credits'] == 25
    assert saved[0]['preferences']['credits'] == 25
    assert '_raw_config' not in saved[0]
    assert raw == {'id': 'child', 'api': 'child-key', 'parent_id': 'parent'}


@pytest.mark.asyncio
async def test_topup_persistence_failure_does_not_change_live_balance(monkeypatch):
    from routes import stats as route, admin
    config = {'api_keys': [{'id': 'k', 'api': 'k', 'preferences': {'credits': 10}}]}
    state = SimpleNamespace(config=config, api_list=['k'],
                            paid_api_keys_states={'k': {'credits': 10, 'total_cost': 0}})
    monkeypatch.setattr(route, 'get_app', lambda: SimpleNamespace(state=state))
    monkeypatch.setattr(admin, '_persist_config', AsyncMock(side_effect=RuntimeError('disk unavailable')))
    with pytest.raises(RuntimeError):
        await route.add_credits_to_api_key(None, paid_key='k', amount=5, token='admin')
    assert state.config is config
    assert state.paid_api_keys_states['k']['credits'] == 10
