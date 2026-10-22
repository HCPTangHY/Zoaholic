"""Plugin management must use the declared name, not the uploaded filename."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.plugins.loader import PluginLoader
from core.plugins.manager import PluginManager
from routes import plugins as plugin_routes


@pytest.fixture
def plugin_file(tmp_path):
    path = tmp_path / "linux_toolcall(10.18).py"
    path.write_text("PLUGIN_INFO = {'name': 'linux_toolcall', 'version': '1'}\n", encoding="utf-8")
    yield path
    sys.modules.pop("zoaholic_plugins.linux_toolcall(10.18)", None)


def test_declared_name_is_registry_key(plugin_file):
    loader = PluginLoader([str(plugin_file.parent)])
    info = loader.load_from_file(str(plugin_file))
    assert info.name == "linux_toolcall"
    assert loader.get_plugin(info.name) is info
    assert list(loader.plugins) == [info.name]
    assert loader.get_plugin(plugin_file.stem) is None


def test_rescan_does_not_create_duplicate(plugin_file):
    loader = PluginLoader([str(plugin_file.parent)])
    info = loader.load_from_file(str(plugin_file))
    assert loader.load_from_directory() == [info]
    assert len(loader.plugins) == 1


def test_reload_and_unload_by_declared_name(plugin_file):
    loader = PluginLoader()
    first = loader.load_from_file(str(plugin_file))
    second = loader.reload_plugin(first.name)
    assert second is not first
    assert list(loader.plugins) == ["linux_toolcall"]
    assert loader.unload_plugin(second.name)
    assert not second.enabled


def test_overwrite_same_file(plugin_file):
    loader = PluginLoader()
    first = loader.load_from_file(str(plugin_file))
    second = loader.load_from_file(str(plugin_file), overwrite=True)
    assert second is not first
    assert not first.enabled
    assert loader.get_plugin("linux_toolcall") is second
    assert len(loader.plugins) == 1


def test_duplicate_declared_name_respects_overwrite(plugin_file):
    other = plugin_file.with_name("linux_new.py")
    other.write_text(plugin_file.read_text(encoding="utf-8"), encoding="utf-8")
    loader = PluginLoader()
    first = loader.load_from_file(str(plugin_file))
    assert loader.load_from_file(str(other)) is first
    second = loader.load_from_file(str(other), overwrite=True)
    assert second.path == str(other)
    assert not first.enabled
    assert list(loader.plugins) == ["linux_toolcall"]
    sys.modules.pop("zoaholic_plugins.linux_new", None)


def test_dynamic_module_uses_declared_name(monkeypatch):
    monkeypatch.setitem(sys.modules, "test_plugin_alias", SimpleNamespace(PLUGIN_INFO={"name": "declared"}))
    loader = PluginLoader()
    info = loader.load_from_module("test_plugin_alias")
    assert loader.get_plugin("declared") is info
    assert loader.unload_plugin("declared")


def test_entry_point_uses_declared_name(monkeypatch):
    ep = SimpleNamespace(name="entry_alias", value="test:plugin", load=lambda: SimpleNamespace(PLUGIN_INFO={"name": "declared"}))
    monkeypatch.setattr("importlib.metadata.entry_points", lambda **kwargs: [ep])
    loader = PluginLoader()
    info, = loader.load_from_entry_points()
    assert loader.get_plugin("declared") is info
    assert loader.unload_plugin("declared")


def test_management_api_accepts_listed_name(plugin_file, monkeypatch):
    manager = PluginManager([str(plugin_file.parent)])
    manager.load_plugin(str(plugin_file), activate=False)
    registry = Mock()
    registry.get_interceptor_plugins.return_value = []
    monkeypatch.setattr(plugin_routes, "get_plugin_manager", lambda: manager)
    monkeypatch.setattr(plugin_routes, "get_interceptor_registry", lambda: registry)
    app = FastAPI()
    app.include_router(plugin_routes.router)
    app.dependency_overrides[plugin_routes.verify_admin_api_key] = lambda: 0
    app.dependency_overrides[plugin_routes.rate_limit_dependency] = lambda: None
    with TestClient(app) as client:
        name = client.get("/v1/plugins").json()["plugins"][0]["name"]
        assert client.post(f"/v1/plugins/{name}/disable").status_code == 200
        assert client.post(f"/v1/plugins/{name}/enable").status_code == 200
        assert client.post(f"/v1/plugins/{name}/reload").status_code == 200
        response = client.delete(f"/v1/plugins/{name}")
        assert response.status_code == 200, response.text
        assert response.json()["success"]
        assert not plugin_file.exists()
        assert client.get("/v1/plugins").json()["plugins"] == []
        assert client.delete(f"/v1/plugins/{name}").status_code == 404
    registry.unregister_plugin_interceptors.assert_any_call(name)
