"""Offline regressions for the security hardening changes.

覆盖：
- core/client_ip 可信代理解析（防 X-Forwarded-For 伪造）
- StatsMiddleware 端到端的客户端 IP 判定
- metrics 连接池标识凭证脱敏
- 公开 /healthz /readyz 最小化载荷与管理员详情拆分
- /setup/status 字段精简
- debug 路由的方法约束、限频与默认关闭开关
- 插件上传 / workspace / OAuth 导出部署级开关

不使用真实凭证，不访问网络。
"""
import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, Request

import core.client_ip as client_ip
from core.client_ip import resolve_client_ip
from core.metrics import _redact_credentials, get_pool_metrics


@pytest.fixture(autouse=True)
def _reset_client_ip_cache(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    client_ip._cached_raw = None
    yield
    client_ip._cached_raw = None


# ==================== core/client_ip ====================

def test_untrusted_peer_ignores_forwarded_headers():
    assert resolve_client_ip("203.0.113.9", forwarded_for="1.2.3.4", real_ip="5.6.7.8") == "203.0.113.9"


def test_trusted_peer_takes_rightmost_untrusted_hop():
    # 客户端伪造了最左值，nginx 追加真实地址在右侧
    assert resolve_client_ip("127.0.0.1", forwarded_for="1.2.3.4, 8.8.8.8") == "8.8.8.8"


def test_trusted_chain_returns_leftmost():
    assert resolve_client_ip("127.0.0.1", forwarded_for="192.168.1.5, 10.0.0.2") == "192.168.1.5"


def test_real_ip_fallback_only_from_trusted_peer():
    assert resolve_client_ip("127.0.0.1", real_ip="9.9.9.9") == "9.9.9.9"
    assert resolve_client_ip("203.0.113.9", real_ip="9.9.9.9") == "203.0.113.9"


def test_docker_bridge_peer_is_trusted_by_default():
    assert resolve_client_ip("172.17.0.1", forwarded_for="8.8.4.4") == "8.8.4.4"


def test_star_config_restores_legacy_trust_all(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "*")
    client_ip._cached_raw = None
    assert resolve_client_ip("203.0.113.9", forwarded_for="1.2.3.4, 5.6.7.8") == "1.2.3.4"


def test_explicit_trusted_list(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXIES", "198.51.100.7")
    client_ip._cached_raw = None
    assert resolve_client_ip("198.51.100.7", forwarded_for="8.8.8.8") == "8.8.8.8"
    # 收紧后回环不再可信
    assert resolve_client_ip("127.0.0.1", forwarded_for="8.8.8.8") == "127.0.0.1"


def test_missing_peer_is_unknown():
    assert resolve_client_ip(None, forwarded_for="8.8.8.8") == "unknown"


# ==================== StatsMiddleware 端到端 ====================

@pytest.fixture
def gateway(monkeypatch):
    import core.middleware as middleware

    app = FastAPI()
    entries = [{"api": "fixture-user-key", "role": "user"}]
    app.state.config = {"api_keys": entries, "preferences": {}}
    app.state.api_keys_db = entries
    app.state.api_list = ["fixture-user-key"]
    app.state.paid_api_keys_states = {}
    app.state.global_rate_limit = [(10000, 60)]
    ready = asyncio.Event()
    ready.set()
    monkeypatch.setitem(sys.modules, "main", SimpleNamespace(_db_ready=ready))
    monkeypatch.setattr(middleware, "DISABLE_DATABASE", True)
    monkeypatch.setattr(middleware, "on_request_start", lambda *a, **k: None)
    monkeypatch.setattr(middleware, "on_request_model", lambda *a, **k: None)
    monkeypatch.setattr(middleware, "on_request_end", lambda *a, **k: None)
    app.add_middleware(middleware.StatsMiddleware)

    @app.get("/v1/test-ip")
    async def show_ip(request: Request):
        return {"client_ip": middleware.request_info.get().get("client_ip")}

    return app


def _call_ip(app, peer, headers):
    async def run():
        transport = httpx.ASGITransport(app=app, client=(peer, 4321))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            all_headers = {"Authorization": "Bearer fixture-user-key"}
            all_headers.update(headers)
            return await c.get("/v1/test-ip", headers=all_headers)
    return asyncio.run(run())


def test_middleware_rejects_spoofed_xff_from_untrusted_peer(gateway):
    resp = _call_ip(gateway, "203.0.113.9", {"X-Forwarded-For": "1.2.3.4"})
    assert resp.status_code == 200
    assert resp.json()["client_ip"] == "203.0.113.9"


def test_middleware_resolves_real_client_behind_trusted_proxy(gateway):
    resp = _call_ip(gateway, "127.0.0.1", {"X-Forwarded-For": "1.2.3.4, 8.8.8.8"})
    assert resp.status_code == 200
    assert resp.json()["client_ip"] == "8.8.8.8"


# ==================== metrics 脱敏 ====================

def test_redact_credentials_strips_userinfo():
    key = "api.example.com_http://alice:s3cret@proxy.internal:1080"
    redacted = _redact_credentials(key)
    assert "s3cret" not in redacted and "alice" not in redacted
    assert redacted == "api.example.com_http://***@proxy.internal:1080"


def test_pool_metrics_redact_proxy_credentials():
    manager = SimpleNamespace(
        clients={"api.example.com_socks5://bob:hunter2@10.0.0.5:1080": SimpleNamespace(_transport=None, _mounts={})},
        pool_size=10,
        max_keepalive_connections=5,
    )
    metrics = get_pool_metrics(manager)
    assert metrics["available"] is True
    assert "hunter2" not in str(metrics)
    assert metrics["pools"][0]["key"].startswith("api.example.com_socks5://***@")


# ==================== 健康探针载荷 ====================

def _fake_health_app():
    state = SimpleNamespace(
        startup_completed=True,
        started_at=None,
        version="9.9.9-test",
        needs_setup=False,
        config={"providers": [{"provider": "p1"}], "api_keys": [{"api": "k"}]},
        client_manager=SimpleNamespace(clients={}, pool_size=1, max_keepalive_connections=1),
        channel_manager=object(),
        event_loop_watchdog=None,
    )
    return SimpleNamespace(state=state)


def test_public_probe_payload_is_minimal():
    from routes.health import _public_probe_payload

    payload, status_code = _public_probe_payload(_fake_health_app(), readiness=False)
    assert status_code == 200
    assert set(payload) == {"status", "probe", "timestamp"}
    text = str(payload)
    assert "9.9.9-test" not in text
    assert "provider" not in text


def test_admin_detail_payload_keeps_diagnostics():
    from routes.health import _build_health_payload

    payload, status_code = _build_health_payload(_fake_health_app(), readiness=True)
    assert status_code == 200
    assert payload["version"] == "9.9.9-test"
    assert "checks" in payload and "metrics" in payload


def test_probe_status_code_semantics_preserved():
    from routes.health import _public_probe_payload

    app = _fake_health_app()
    app.state.startup_completed = False
    del app.state.client_manager
    # healthz 仍 200（存活），readyz 503（未就绪）
    assert _public_probe_payload(app, readiness=False)[1] == 200
    assert _public_probe_payload(app, readiness=True)[1] == 503


# ==================== setup 状态精简 ====================

def test_setup_status_only_exposes_needs_setup():
    from routes.setup import SetupStatus

    assert set(SetupStatus.model_fields) == {"needs_setup"}


# ==================== debug 路由 ====================

@pytest.fixture
def debug_app(monkeypatch):
    import routes.debug as debug_routes
    from routes.deps import verify_admin_api_key

    monkeypatch.setattr(debug_routes, "_last_expensive_call", 0.0)
    app = FastAPI()
    app.include_router(debug_routes.router)
    app.dependency_overrides[verify_admin_api_key] = lambda: "fixture-admin"
    return app


def _req(app, method, path):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.request(method, path)
    return asyncio.run(run())


def test_tracemalloc_toggle_requires_post(debug_app):
    assert _req(debug_app, "GET", "/debug/memory/tracemalloc/start").status_code == 405
    assert _req(debug_app, "GET", "/debug/memory/tracemalloc/stop").status_code == 405
    assert _req(debug_app, "POST", "/debug/memory/tracemalloc/start").status_code == 200
    assert _req(debug_app, "POST", "/debug/memory/tracemalloc/stop").status_code == 200


def test_expensive_debug_endpoints_are_throttled(debug_app, monkeypatch):
    import routes.debug as debug_routes

    monkeypatch.setattr(debug_routes, "DEBUG_MIN_INTERVAL", 60.0)
    assert _req(debug_app, "GET", "/debug/memory").status_code == 200
    assert _req(debug_app, "GET", "/debug/memory").status_code == 429


def test_debug_requires_admin_credentials():
    import routes.debug as debug_routes

    app = FastAPI()
    app.include_router(debug_routes.router)
    assert _req(app, "GET", "/debug/memory").status_code == 403


def test_debug_router_not_registered_by_default(monkeypatch):
    """行为级验证：默认不注册 /debug/*（404）；开启后注册但仍需管理员凭证（403）。

    说明：新版 FastAPI 的 include_router 为惰性注册，无法直接检查子路由 path，
    故通过实际请求断言。
    """
    import importlib
    import routes as routes_pkg

    def _status(pkg):
        app = FastAPI()
        app.include_router(pkg.api_router)
        return _req(app, "GET", "/debug/memory").status_code

    monkeypatch.delenv("ENABLE_DEBUG_ENDPOINTS", raising=False)
    assert _status(importlib.reload(routes_pkg)) == 404

    monkeypatch.setenv("ENABLE_DEBUG_ENDPOINTS", "true")
    assert _status(importlib.reload(routes_pkg)) == 403  # 已注册，但无凭证被拒

    monkeypatch.delenv("ENABLE_DEBUG_ENDPOINTS", raising=False)
    importlib.reload(routes_pkg)


# ==================== 高权限功能开关 ====================

def test_workspace_api_can_be_disabled(monkeypatch):
    import routes.workspace as workspace_routes
    from routes.deps import rate_limit_dependency, verify_admin_api_key

    app = FastAPI()
    app.include_router(workspace_routes.router)
    app.dependency_overrides[verify_admin_api_key] = lambda: "fixture-admin"
    app.dependency_overrides[rate_limit_dependency] = lambda: None

    monkeypatch.setenv("ENABLE_WORKSPACE_API", "false")
    assert _req(app, "GET", "/v1/workspace/tree").status_code == 404

    monkeypatch.delenv("ENABLE_WORKSPACE_API", raising=False)
    assert _req(app, "GET", "/v1/workspace/tree").status_code == 200


def test_plugin_upload_can_be_disabled(monkeypatch):
    import routes.plugins as plugin_routes
    from routes.deps import rate_limit_dependency, verify_admin_api_key

    app = FastAPI()
    app.include_router(plugin_routes.router)
    app.dependency_overrides[verify_admin_api_key] = lambda: 0
    app.dependency_overrides[rate_limit_dependency] = lambda: None

    monkeypatch.setenv("ENABLE_PLUGIN_UPLOAD", "false")

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(
                "/v1/plugins/upload",
                files={"file": ("fixture.py", b"# noop plugin\n", "text/x-python")},
            )
    assert asyncio.run(run()).status_code == 403


def test_oauth_export_can_be_disabled(monkeypatch):
    import routes.oauth as oauth_routes
    from routes.deps import verify_admin_api_key

    app = FastAPI()
    app.include_router(oauth_routes.router)
    app.dependency_overrides[verify_admin_api_key] = lambda: "fixture-admin"

    monkeypatch.setenv("ENABLE_OAUTH_EXPORT", "false")
    resp = _req(app, "GET", "/v1/oauth/export?provider=demo")
    assert resp.status_code == 403
