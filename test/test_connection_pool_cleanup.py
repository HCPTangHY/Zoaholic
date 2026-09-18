import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks

from core.client_manager import ClientManager
from core.dialects.registry import EndpointDefinition
from core.dialects.router import _create_generic_handler
from core.stream_pipeline import iter_sse_with_keepalive
from core.streaming import LoggingStreamingResponse


@pytest.mark.asyncio
async def test_client_manager_keeps_pool_timeout_short_when_request_read_timeout_is_long():
    captured = {}

    async def upstream(request):
        captured.update(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    manager = ClientManager()
    await manager.init({"transport": httpx.MockTransport(upstream)})
    try:
        async with manager.get_client("https://example.com") as client:
            response = await client.get("https://example.com/test", timeout=900)
        assert response.status_code == 200
        assert captured["read"] == 900
        assert captured["pool"] == 10.0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_dialect_stream_closes_inner_iterator_when_outer_stream_is_closed(monkeypatch):
    closed = asyncio.Event()

    async def inner_stream():
        try:
            yield "data: {}\n\n"
            await asyncio.sleep(3600)
        finally:
            closed.set()

    class ModelHandler:
        async def request_model(self, **kwargs):
            return LoggingStreamingResponse(inner_stream(), media_type="text/event-stream")

    monkeypatch.setattr("routes.deps.get_model_handler", lambda: ModelHandler())
    endpoint = EndpointDefinition(path="/v1/chat/completions", methods=["POST"])
    handler = _create_generic_handler("openai", endpoint)
    request = SimpleNamespace(
        json=lambda: _async_value({"model": "test", "messages": [], "stream": True}),
        headers={},
        path_params={},
        url=SimpleNamespace(path="/v1/chat/completions"),
    )

    response = await handler(request, BackgroundTasks(), api_index=0)
    assert await response.body_iterator.__anext__() == "data: {}\n\n"
    await response.body_iterator.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_keepalive_wait_task_finishes_cancellation_cleanup():
    closed = asyncio.Event()

    async def source():
        try:
            await asyncio.sleep(3600)
            yield "unused"
        finally:
            closed.set()

    stream = iter_sse_with_keepalive(source(), interval=0.01)
    assert await stream.__anext__() == ": keepalive\n\n"
    await stream.aclose()
    await asyncio.wait_for(closed.wait(), timeout=1)


async def _async_value(value):
    return value
