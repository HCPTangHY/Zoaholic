import json

import pytest

from core.middleware import request_info
from core.response import check_response
from db import RequestStat
from routes.stats import DEFAULT_LOG_CLEANUP_FIELDS, LOG_CLEARABLE_FIELDS, LogEntry


class FakeHttpxResponse:
    def __init__(self, status_code=200):
        # 修改原因：本轮要记录上游返回的响应头，同时必须过滤敏感头。
        # 修改方式：测试使用大小写混合的响应头，覆盖大小写不敏感过滤逻辑。
        # 目的：防止 Set-Cookie 或 X-API-Key 被写入可查看的日志详情。
        self.status_code = status_code
        self.headers = {
            "Content-Type": "application/json",
            "Set-Cookie": "session=secret",
            "X-API-Key": "secret-key",
            "X-Upstream-Trace": "trace-id",
        }

    async def aread(self):
        return b'{"ok":true}'

    async def aiter_text(self):
        if False:
            yield ""

    async def aiter_bytes(self):
        if False:
            yield b""


@pytest.mark.asyncio
async def test_check_response_saves_filtered_upstream_response_headers_for_error():
    # 修改原因：非 2xx 响应会提前读取响应体，过去只保存响应体而没有保存响应头。
    # 修改方式：通过 check_response 的错误分支断言响应头与响应体一起写入上下文。
    # 目的：保证上游失败时日志详情也能查看经过脱敏的 response headers。
    info = {"raw_data_expires_at": object()}
    token = request_info.set(info)
    try:
        response = FakeHttpxResponse(status_code=502)

        error = await check_response(response, "test")

        saved_headers = json.loads(info["upstream_response_headers"])
        assert error["status_code"] == 502
        assert saved_headers == {
            "Content-Type": "application/json",
            "X-Upstream-Trace": "trace-id",
        }
        assert "upstream_response_body" in info
    finally:
        request_info.reset(token)


@pytest.mark.asyncio
async def test_check_response_saves_filtered_upstream_response_headers_for_success_aread():
    # 修改原因：成功的非流式响应通过 wrapped aread 保存响应体，也需要同步保存响应头。
    # 修改方式：先让 check_response 包装 response，再读取 aread 触发采集逻辑。
    # 目的：保证普通成功请求的上游响应头能进入 request_stats。
    info = {"raw_data_expires_at": object()}
    token = request_info.set(info)
    try:
        response = FakeHttpxResponse(status_code=200)

        error = await check_response(response, "test")
        body = await response.aread()

        saved_headers = json.loads(info["upstream_response_headers"])
        assert error is None
        assert body == b'{"ok":true}'
        assert saved_headers == {
            "Content-Type": "application/json",
            "X-Upstream-Trace": "trace-id",
        }
    finally:
        request_info.reset(token)


def test_log_schema_exposes_upstream_header_fields():
    # 修改原因：前端只有在日志接口返回字段后，才能展示上游请求头和上游响应头。
    # 修改方式：断言 ORM 列、Pydantic 响应模型、清理白名单都包含新增字段。
    # 目的：防止只写入数据库但接口或清理配置遗漏字段。
    assert "upstream_response_headers" in RequestStat.__table__.columns
    assert "upstream_request_headers" in LogEntry.model_fields
    assert "upstream_response_headers" in LogEntry.model_fields
    assert LOG_CLEARABLE_FIELDS["upstream_response_headers"] == "上游响应头(upstream_response_headers)"
    assert "upstream_response_headers" in DEFAULT_LOG_CLEANUP_FIELDS
