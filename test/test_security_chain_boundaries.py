"""Regression of the reported user-file URL chain without any network or model call."""
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException

from core.auth import verify_api_key
from core.channels.claude_channel import get_claude_payload
from core.models import RequestModel
from core import file_utils


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,expected", [(False, 503), (True, 400)])
async def test_user_chat_file_cannot_reach_loopback_before_or_after_setup(monkeypatch, ready, expected):
    async def forbidden_network_client():
        pytest.fail("Unsafe file URL reached the download client")

    monkeypatch.setattr(file_utils, "_get_shared_fetch_client", forbidden_network_client)
    app = FastAPI()
    app.state.admin_initialized = ready
    app.state.api_list = ["admin-test-key", "user-test-key"]
    app.state.api_keys_db = [
        {"id": "admin", "api": "admin-test-key", "role": "admin"},
        {"id": "user", "api": "user-test-key", "role": "user", "model": ["all"]},
    ]
    app.state.config = {"api_keys": app.state.api_keys_db}
    provider = {"provider": "isolated", "base_url": "https://upstream.invalid", "model": ["claude-test"]}
    reached_adapter = []

    @app.post("/v1/chat/completions")
    async def chat(payload: RequestModel, api_index: int = Depends(verify_api_key)):
        assert api_index == 1
        reached_adapter.append(True)
        await get_claude_payload(payload, "claude", provider, "not-a-real-upstream-key")
        pytest.fail("Unsafe request reached the model-send boundary")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test") as client:
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer user-test-key"}, json={
            "model": "claude-test", "stream": False,
            "messages": [{"role": "user", "content": [{"type": "file", "file": {
                "url": "http://127.0.0.1:8101/..%2fharmless-marker.txt",
            }}]}],
        })
    assert response.status_code == expected
    assert bool(reached_adapter) == ready
    assert "127.0.0.1" not in response.text


def test_workspace_sibling_prefix_symlink_is_not_inside_root(tmp_path, monkeypatch):
    from routes import workspace

    root = tmp_path / "project"
    sibling = tmp_path / "project-other"
    (root / "data").mkdir(parents=True)
    sibling.mkdir()
    (sibling / "marker.txt").write_text("outside")
    (root / "data" / "external").symlink_to(sibling, target_is_directory=True)
    monkeypatch.setattr(workspace, "PROJECT_ROOT", root)
    with pytest.raises(HTTPException) as error:
        workspace._resolve_path("data/external/marker.txt")
    assert error.value.status_code == 403
