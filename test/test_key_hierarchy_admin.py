from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core.key_hierarchy import prepare_key_hierarchy
from routes import admin


@pytest.fixture
def app(monkeypatch):
    keys = prepare_key_hierarchy([
        {"id": "root", "api": "zk-parent", "name": "Parent", "model": ["allowed"]},
        {"id": "child", "api": "zk-child", "parent_id": "root"},
    ])
    instance = SimpleNamespace(state=SimpleNamespace(config={"providers": [], "api_keys": keys}))
    monkeypatch.setattr(admin, "get_app", lambda: instance)
    monkeypatch.setattr(admin, "_persist_config", AsyncMock(return_value={"verified": True}))
    return instance


@pytest.mark.asyncio
async def test_bad_relation_does_not_mutate_any_section(app):
    original = deepcopy(app.state.config)
    with pytest.raises(HTTPException) as error:
        await admin.api_config_update(0, {"providers": [{"provider": "new"}], "api_keys": [
            {"id": "root", "api": "zk-parent", "parent_id": "child"},
            {"id": "child", "api": "zk-child", "parent_id": "root"},
        ]})
    assert error.value.status_code == 400
    assert app.state.config == original
    admin._persist_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_client_preserves_stable_id_and_raw_child(app):
    await admin.api_config_update(0, {"api_keys": [
        {"api": "zk-parent", "model": ["new"]},
        {"api": "zk-child", "parent_id": "root"},
    ]})
    candidate = admin._persist_config.await_args.kwargs['candidate_config']
    assert [key["id"] for key in candidate["api_keys"]] == ["root", "child"]
    assert "model" not in candidate["api_keys"][1]
    assert app.state.config['api_keys'][0]['model'] == ['allowed']


@pytest.mark.asyncio
async def test_refuses_existing_id_replacement_and_parent_deletion(app):
    for keys in ([{"id": "other", "api": "zk-parent"}], [{"id": "child", "api": "zk-child"}]):
        with pytest.raises(HTTPException):
            await admin.api_config_update(0, {"api_keys": keys, "replace_all": True})
    # Deleting a branch in one operation also requires explicit leaf-first deletion.
    with pytest.raises(HTTPException):
        await admin.api_config_update(0, {"api_keys": [], "replace_all": True})


@pytest.mark.asyncio
async def test_single_child_creation_does_not_materialize_parent_policy(app):
    from core.key_hierarchy import raw_key_config
    keys = [raw_key_config(key) for key in app.state.config['api_keys']]
    keys.append({'api': 'zk-new-child', 'parent_id': 'root', 'name': 'New child'})
    await admin.api_config_update(0, {'api_keys': keys})
    created = admin._persist_config.await_args.kwargs['candidate_config']['api_keys'][-1]
    assert created['id']
    assert created['parent_id'] == 'root'
    assert 'model' not in created
    assert 'preferences' not in created


@pytest.mark.asyncio
async def test_removed_batch_endpoint_is_not_available():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    application = FastAPI()
    application.include_router(admin.router)
    async with AsyncClient(transport=ASGITransport(app=application), base_url='http://test') as client:
        response = await client.post('/v1/api_keys/batch', json={'parent_id': 'root', 'names': ['One']})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_config_exposes_raw_and_noncredential_effective_values(app):
    import json
    response = await admin.api_config(0)
    data = json.loads(response.body)
    assert "model" not in data["api_config"]["api_keys"][1]
    assert data["effective_keys"]["child"]["model"] == ["allowed"]
    assert "api" not in data["effective_keys"]["child"]


@pytest.fixture
def persistence_app(monkeypatch):
    import sys
    keys = prepare_key_hierarchy([
        {'id': 'root', 'api': 'zk-parent', 'model': ['old']},
        {'id': 'child', 'api': 'zk-child', 'parent_id': 'root'},
    ])
    old = {'providers': [], 'api_keys': keys, 'preferences': {}}
    instance = SimpleNamespace(state=SimpleNamespace(config=old, api_keys_db=keys,
                                                    api_list=['zk-parent', 'zk-child']))
    monkeypatch.setenv('CONFIG_STORAGE', 'db')
    monkeypatch.delenv('SYNC_CONFIG_TO_FILE', raising=False)
    monkeypatch.setitem(sys.modules, 'main', SimpleNamespace(
        init_preference=lambda config, *args, **kwargs: {}, DEFAULT_TIMEOUT=60,
        DEFAULT_KEEPALIVE_INTERVAL=10, DEFAULT_STREAM_HEADER_TIMEOUT=30))
    monkeypatch.setattr(admin, 'apply_backend_log_preferences', lambda prefs: None)
    return instance


@pytest.mark.asyncio
async def test_persist_keeps_old_runtime_visible_while_storage_pending(persistence_app, monkeypatch):
    import asyncio
    from core.config import db_store
    from core.key_hierarchy import raw_key_config
    instance = persistence_app
    old_config = instance.state.config
    old_keys = instance.state.api_keys_db
    candidate = {'providers': [], 'api_keys': [raw_key_config(k) for k in old_keys]}
    candidate['api_keys'][0]['model'] = ['new']
    entered, resume = asyncio.Event(), asyncio.Event()

    async def save(config):
        assert config['api_keys'][1]['model'] == ['new']
        entered.set()
        await resume.wait()
    monkeypatch.setattr(db_store, 'save_config_to_db', save)
    task = asyncio.create_task(admin._persist_config(instance, ['api_keys'], candidate_config=candidate))
    await entered.wait()
    assert instance.state.config is old_config
    assert instance.state.api_keys_db is old_keys
    assert instance.state.config['api_keys'][1]['model'] == ['old']
    resume.set()
    await task
    assert instance.state.config is not old_config
    assert instance.state.api_keys_db is instance.state.config['api_keys']
    assert instance.state.config['api_keys'][1]['model'] == ['new']


@pytest.mark.asyncio
async def test_storage_failure_does_not_publish_any_runtime_fields(persistence_app, monkeypatch):
    from core.config import db_store
    original_fields = vars(persistence_app.state).copy()
    async def fail(config):
        raise OSError('storage unavailable')
    monkeypatch.setattr(db_store, 'save_config_to_db', fail)
    candidate = {'providers': [], 'api_keys': [{'api': 'replacement'}]}
    with pytest.raises(HTTPException) as error:
        await admin._persist_config(persistence_app, ['api_keys'], candidate_config=candidate)
    assert error.value.status_code == 500
    assert vars(persistence_app.state).keys() == original_fields.keys()
    for name, value in original_fields.items():
        assert getattr(persistence_app.state, name) is value


@pytest.mark.asyncio
@pytest.mark.parametrize('prefs', [[], {'quota': []}, {'quota': {'token': 'nonsense'}},
                                   {'quota_rules': [{'limit': 'broken'}]}, {'rate_limit': 5}])
async def test_invalid_preferences_never_reach_storage(persistence_app, monkeypatch, prefs):
    from core.config import db_store
    save = AsyncMock()
    monkeypatch.setattr(db_store, 'save_config_to_db', save)
    with pytest.raises(HTTPException) as error:
        await admin._persist_config(persistence_app, ['api_keys'], candidate_config={
            'providers': [], 'api_keys': [{'api': 'new', 'preferences': prefs}]})
    assert error.value.status_code == 400
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_file_verification_failure_keeps_old_runtime(persistence_app, monkeypatch, tmp_path):
    from core.config import file_store
    monkeypatch.setenv('CONFIG_STORAGE', 'file')
    path = tmp_path / 'api.yaml'
    path.write_text('providers: []\napi_keys: []\n', encoding='utf-8')
    monkeypatch.setattr(admin, 'API_YAML_PATH', str(path))
    monkeypatch.setattr(file_store, 'save_api_yaml', lambda config: None)
    old = persistence_app.state.config
    with pytest.raises(HTTPException) as error:
        await admin._persist_config(persistence_app, ['api_keys'], candidate_config={
            'providers': [], 'api_keys': [{'api': 'new'}]})
    assert error.value.status_code == 500
    assert persistence_app.state.config is old
    assert persistence_app.state.api_keys_db is old['api_keys']
