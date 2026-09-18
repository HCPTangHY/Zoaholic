import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# 修改原因：单独运行本测试文件时，pytest 当前路径不一定指向 Zoaholic 项目根目录。
# 修改方式：从测试文件向上查找包含 core/ 和 routes/ 的目录，并把它加入 sys.path。
# 目的：保证批量 OAuth 导入测试在完整测试集和单文件运行时都能稳定导入 routes.oauth。
ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _json_response_payload(response) -> dict:
    """读取 FastAPI JSONResponse 的 JSON 内容。"""
    # 修改原因：路由函数测试直接调用 batch_import，不经过 ASGI 客户端时也要检查错误响应。
    # 修改方式：把 JSONResponse.body 解码后交给 json.loads。
    # 目的：让测试保持轻量，同时覆盖路由返回 JSONResponse 的分支。
    return json.loads(response.body.decode())


def test_normalize_batch_import_accepts_sub2api_and_cpa_formats():
    from routes.oauth import _normalize_batch_import_data

    # 修改原因：新批量导入端点需要同时接受 sub2api、CPA 单文件和 CPA 多文件合并格式。
    # 修改方式：直接调用 normalize 函数，断言 key_id、source_format 和过期时间转换结果。
    # 目的：先固定格式识别规则，避免后续路由处理时把导出数据写成错误的 OAuth state。
    sub2api_items = _normalize_batch_import_data({
        "accounts": [
            {
                "name": "alice@gmail.com",
                "credentials": {
                    "access_token": "access-sub2api",
                    "refresh_token": "refresh-sub2api",
                    "expires_at": 1749120000,
                    "email": "alice-creds@gmail.com",
                },
            }
        ]
    })
    assert sub2api_items == [
        {
            "key_id": "alice@gmail.com",
            "source_format": "sub2api",
            "token_data": {
                "access_token": "access-sub2api",
                "refresh_token": "refresh-sub2api",
                "expires_at": datetime.fromtimestamp(1749120000, timezone.utc).isoformat().replace("+00:00", "Z"),
                "email": "alice-creds@gmail.com",
            },
        }
    ]

    cpa_items = _normalize_batch_import_data([
        {
            "access_token": "access-codex",
            "refresh_token": "refresh-codex",
            "expires_at": "2026-01-01T00:00:00Z",
            "user": {"id": "codex-user-1", "email": "codex@example.com"},
        },
        {
            "access_token": "access-claude",
            "refresh_token": "refresh-claude",
            "expires_in": 3600,
            "organization": {"uuid": "org-1", "name": "Org"},
            "account": {"uuid": "acct-1", "email_address": "claude@example.com"},
        },
        {
            "access_token": "access-gemini",
            "refresh_token": "refresh-gemini",
            "token_type": "Bearer",
            "expiry": "2026-02-01T00:00:00Z",
            "email": "gemini@example.com",
        },
    ])

    assert [item["key_id"] for item in cpa_items] == ["codex@example.com", "claude@example.com", "gemini@example.com"]
    assert [item["source_format"] for item in cpa_items] == ["cpa_codex", "cpa_claude", "cpa_gemini"]
    assert cpa_items[0]["token_data"]["email"] == "codex@example.com"
    assert cpa_items[0]["token_data"]["account_id"] == "codex-user-1"
    assert cpa_items[1]["token_data"]["organization_id"] == "org-1"
    assert cpa_items[1]["token_data"]["organization_name"] == "Org"
    assert cpa_items[1]["token_data"]["account_id"] == "acct-1"
    assert isinstance(cpa_items[1]["token_data"]["expires_at"], float)
    assert cpa_items[2]["token_data"]["expires_at"] == "2026-02-01T00:00:00Z"


@pytest.mark.asyncio
async def test_batch_import_continues_after_refresh_failure_and_marks_existing_account():
    from routes.oauth import batch_import

    # 修改原因：批量导入不能因为单个 refresh_token 失效而中断整批账号。
    # 修改方式：使用假的 OAuthManager 和 provider，构造成功刷新、刷新失败、缺少 access_token 三类记录。
    # 目的：固定逐条记录结果、覆盖旧凭据和继续处理后续账号的行为。
    class Provider:
        async def refresh_token(self, credential: dict) -> dict:
            if credential.get("refresh_token") == "bad-refresh":
                raise RuntimeError("refresh_token expired")
            updated = dict(credential)
            updated["access_token"] = "access-new"
            updated["refresh_token"] = "refresh-new"
            updated["email"] = "ok@example.com"
            return updated

    class OAuthManager:
        def __init__(self):
            self._providers = {"codex": Provider()}
            self.register_calls = []

        async def refresh_provider(self, type_name: str, credential: dict) -> dict:
            return await self._providers[type_name].refresh_token(credential)

        async def register(self, channel_id: str, key_id: str, type_name: str, token_data: dict):
            self.register_calls.append((channel_id, key_id, type_name, token_data))

    class Request:
        def __init__(self):
            state = type(
                "State",
                (),
                {
                    "oauth_manager": OAuthManager(),
                    "config": {"providers": [{"provider": "Codex-Main", "api": ["ok@example.com"]}]},
                },
            )()
            self.app = type("App", (), {"state": state})()

        async def json(self):
            return {
                "provider": "Codex-Main",
                "type": "codex",
                "data": [
                    {
                        "access_token": "access-old",
                        "refresh_token": "good-refresh",
                        "expires_at": "2026-01-01T00:00:00Z",
                        "user": {"email": "old@example.com"},
                    },
                    {
                        "access_token": "access-bad",
                        "refresh_token": "bad-refresh",
                        "expires_at": "2026-01-01T00:00:00Z",
                        "user": {"email": "bad@example.com"},
                    },
                    {
                        "key_id": "missing-access@example.com",
                    },
                ],
            }

    request = Request()
    response = await batch_import(request)

    # 修改原因：batch_import 改为 NDJSON 流式返回逐条进度，测试需要解析事件流而不是读取最终 dict。
    # 修改方式：直接迭代 StreamingResponse.body_iterator，逐行 json.loads，还原 progress/item/summary 事件序列。
    # 目的：固定流式协议的行为（事件类型、字段、计数汇总）供前端消费。
    assert response.media_type == "application/x-ndjson"
    events = []
    async for chunk in response.body_iterator:
        for line in chunk.splitlines():
            if line.strip():
                events.append(json.loads(line))

    assert [e["type"] for e in events] == [
        "progress", "item",
        "progress", "item",
        "progress", "item",
        "summary",
    ]
    items = [e for e in events if e["type"] == "item"]
    assert items[0] == {"type": "item", "index": 0, "total": 3, "key_id": "ok@example.com", "status": "success", "already_exists": True}
    assert items[1] == {"type": "item", "index": 1, "total": 3, "key_id": "bad@example.com", "status": "failed", "error": "refresh_token expired"}
    assert items[2] == {"type": "item", "index": 2, "total": 3, "key_id": "missing-access@example.com", "status": "skipped", "error": "missing access_token"}
    assert events[-1] == {"type": "summary", "total": 3, "success": 1, "failed": 1, "skipped": 1}
    assert request.app.state.oauth_manager.register_calls == [
        (
            "Codex-Main",
            "ok@example.com",
            "codex",
            {
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_at": "2026-01-01T00:00:00Z",
                "user": {"email": "old@example.com"},
                "email": "ok@example.com",
            },
        )
    ]
