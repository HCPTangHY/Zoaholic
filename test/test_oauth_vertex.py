import json
import time
from pathlib import Path
import sys

import pytest

# 修改原因：单文件运行本测试时，pytest 的当前导入路径不一定包含项目根目录。
# 修改方式：从当前测试文件向上查找同时包含 core/ 和 routes/ 的目录，并插入 sys.path。
# 目的：让 Vertex OAuth 回归测试在完整测试集和单文件运行两种方式下都能稳定导入项目模块。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_response_payload(response) -> dict:
    """读取 FastAPI JSONResponse 的 JSON 内容。"""
    # 修改原因：路由函数测试直接调用 import_account，不通过 ASGI 客户端也要断言错误或结果内容。
    # 修改方式：对 JSONResponse.body 解码后用 json.loads 还原为字典。
    # 目的：保持测试轻量，同时能覆盖路由返回 JSONResponse 的分支。
    return json.loads(response.body.decode())


@pytest.mark.asyncio
async def test_vertex_provider_refreshes_service_account_token(monkeypatch):
    from core.channels import vertex_channel

    calls = []

    async def fake_get_access_token(client_email: str, private_key: str) -> str:
        calls.append((client_email, private_key))
        return "ya29.vertex-access"

    # 修改原因：VertexProvider.refresh_token 应只负责凭据结构和过期时间，不应在单元测试中访问 Google OAuth 服务。
    # 修改方式：替换 get_access_token 为可控异步函数，并断言 refresh_token 会写回 access_token 与 expires_at。
    # 目的：固定 service account JSON 导入后的刷新行为，避免后续改动破坏 OAuthManager.resolve 的输入数据。
    monkeypatch.setattr(vertex_channel, "get_access_token", fake_get_access_token)
    provider = vertex_channel.VertexProvider()
    before = int(time.time())

    updated = await provider.refresh_token({
        "client_email": "sa@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        "project_id": "vertex-project",
    })

    assert calls == [("sa@example.iam.gserviceaccount.com", "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n")]
    assert updated["access_token"] == "ya29.vertex-access"
    assert before + 3490 <= updated["expires_at"] <= before + 3510
    assert updated["project_id"] == "vertex-project"


@pytest.mark.asyncio
async def test_oauth_import_accepts_vertex_service_account_json():
    from routes.oauth import import_account

    class Provider:
        async def refresh_token(self, credential: dict) -> dict:
            assert credential == {
                "client_email": "sa@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
                "project_id": "vertex-project",
                "email": "sa@example.iam.gserviceaccount.com",
            }
            updated = dict(credential)
            updated["access_token"] = "ya29.vertex-access"
            updated["expires_at"] = 1234567890
            return updated

    class OAuthManager:
        def __init__(self):
            self._providers = {"vertex-gemini": Provider()}
            self.register_calls = []

        async def register(self, channel_id: str, key_id: str, type_name: str, token_data: dict):
            self.register_calls.append((channel_id, key_id, type_name, token_data))

    class Request:
        def __init__(self):
            self.app = type("App", (), {"state": type("State", (), {"oauth_manager": OAuthManager()})()})()

        async def json(self):
            return {
                "provider": "Vertex-Main",
                "type": "vertex-gemini",
                "service_account_json": {
                    "type": "service_account",
                    "project_id": "vertex-project",
                    "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
                    "client_email": "sa@example.iam.gserviceaccount.com",
                },
            }

    # 修改原因：Vertex service account 导入时不应要求前端额外提供 key_id，账号标识应稳定使用 client_email。
    # 修改方式：用假的 OAuthManager 验证路由会先刷新验证凭据，再按渠道名和邮箱写入 oauth_state。
    # 目的：保证 /v1/oauth/import 可以直接粘贴完整 service account JSON 并得到可解析的 OAuth 账号。
    request = Request()
    result = await import_account(request)

    assert result == {"message": "Service account imported", "key_id": "sa@example.iam.gserviceaccount.com"}
    assert request.app.state.oauth_manager.register_calls == [(
        "Vertex-Main",
        "sa@example.iam.gserviceaccount.com",
        "vertex-gemini",
        {
            "client_email": "sa@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
            "project_id": "vertex-project",
            "email": "sa@example.iam.gserviceaccount.com",
            "access_token": "ya29.vertex-access",
            "expires_at": 1234567890,
        },
    )]


def test_token_data_from_body_excludes_service_account_json():
    from routes.oauth import _token_data_from_body

    # 修改原因：service_account_json 是导入控制字段，不能在普通 token_data 中重复保存完整 JSON 包装。
    # 修改方式：直接调用剥离函数，断言它继续保留真实凭据字段，但排除 service_account_json。
    # 目的：避免手动导入分支把控制字段写进 oauth_state，减少敏感数据冗余和后续解析歧义。
    assert _token_data_from_body({
        "provider": "Vertex-Main",
        "type": "vertex-gemini",
        "key_id": "sa@example.iam.gserviceaccount.com",
        "refresh_token": "refresh",
        "service_account_json": {"client_email": "sa@example.iam.gserviceaccount.com"},
    }) == {"refresh_token": "refresh"}


@pytest.mark.asyncio
async def test_vertex_payload_uses_oauth_access_token_and_project_id_from_request_context():
    from core.channels.vertex_channel import get_vertex_gemini_payload
    from core.middleware import request_info
    from core.models import RequestModel

    request = RequestModel(
        model="gemini-test",
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
    )
    provider = {
        "provider": "Vertex-Main",
        "engine": "vertex-gemini",
        "base_url": "https://aiplatform.googleapis.com",
        "model": [{"gemini-2.5-flash": "gemini-test"}],
    }

    # 修改原因：OAuthManager.resolve 只把 key_id 解析成 access_token，project_id 必须通过请求上下文元数据传给 Vertex payload。
    # 修改方式：在 request_info 中放入 OAuth 凭据元数据，并传入 resolved access_token 模拟真实请求路径。
    # 目的：防止 service account JSON 中的 project_id 因 resolve 只返回字符串而在构建 Vertex URL 时丢失。
    token = request_info.set({
        "_oauth_resolved": True,
        "_oauth_credential_metadata": {"project_id": "vertex-project"},
    })
    try:
        url, headers, payload = await get_vertex_gemini_payload(
            request,
            "vertex-gemini",
            provider,
            api_key="ya29.vertex-access",
        )
    finally:
        request_info.reset(token)

    assert headers["Authorization"] == "Bearer ya29.vertex-access"
    assert "/projects/vertex-project/" in url
    assert "key=ya29.vertex-access" not in url
    assert payload["contents"][0]["parts"] == [{"text": "hello"}]
