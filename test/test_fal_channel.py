"""fal.ai 渠道适配器测试。

修改原因：新增 fal.ai 渠道前先固定请求转换、结果格式化和流式队列行为。
修改方式：使用 RequestModel 与轻量假 HTTP 客户端覆盖同步和队列路径，不依赖真实 fal.ai API。
目的：防止后续实现误用 Bearer 鉴权、结构化 content items 或遗漏队列流式输出。
"""

import asyncio
import sys
from pathlib import Path

# 修改原因：本测试会被单文件运行，当前工作目录不一定已经位于 Python 导入路径。
# 修改方式：从测试文件向上查找包含 core/ 的项目根目录，并插入 sys.path。
# 目的：确保测试导入的是本仓库中的 core 包，而不是依赖运行目录。
ROOT = next(parent for parent in Path(__file__).resolve().parents if (parent / "core").is_dir())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.json_utils import json_dumps_text
from core.models import RequestModel


class _FakeResponse:
    """为 fal 渠道测试提供最小 httpx.Response 兼容对象。"""

    def __init__(self, data, status_code=200):
        # 修改原因：check_response 需要读取 status_code 和 headers。
        # 修改方式：保存测试指定的响应体和状态码，并提供空响应头。
        # 目的：让适配器测试专注于 fal 协议转换而非真实网络。
        self._data = data
        self.status_code = status_code
        self.headers = {}

    async def aread(self):
        # 修改原因：项目响应层通过 aread 记录上游响应体。
        # 修改方式：将测试字典序列化为 UTF-8 JSON 字节。
        # 目的：模拟真实 httpx 响应的读取方式。
        return json_dumps_text(self._data).encode("utf-8")

    async def aiter_text(self):
        # 修改原因：check_response 会包装流式文本迭代器，假响应也需要提供同名接口。
        # 修改方式：以异步生成器形式返回完整 JSON 文本。
        # 目的：让测试响应对象覆盖项目响应层依赖的最小协议。
        yield json_dumps_text(self._data)

    async def aiter_bytes(self):
        # 修改原因：check_response 会包装字节迭代器，缺失该接口会让适配器测试停在测试替身上。
        # 修改方式：复用 aread 的字节内容并作为单个 chunk 输出。
        # 目的：模拟 httpx.Response 的流式字节读取能力。
        yield await self.aread()


class _FakeFalClient:
    """记录 fal 适配器发出的请求，并按 URL 返回预置响应。"""

    def __init__(self):
        # 修改原因：测试需要确认队列模式调用了提交、状态和结果三个端点。
        # 修改方式：分别记录 POST 与 GET 的 URL。
        # 目的：验证 stream adapter 的队列流程没有退回同步端点。
        self.post_urls = []
        self.get_urls = []

    async def post(self, url, headers=None, content=None, timeout=None):
        self.post_urls.append(url)
        if "queue.fal.run" in url:
            return _FakeResponse({"request_id": "req_123"})
        return _FakeResponse({"images": [{"url": "https://cdn.example/sync.png"}], "seed": 7})

    async def get(self, url, headers=None, timeout=None):
        self.get_urls.append(url)
        if url.endswith("/status?logs=1"):
            return _FakeResponse({"status": "COMPLETED"})
        return _FakeResponse({"video": {"url": "https://cdn.example/video.mp4"}, "seed": 9})


class _FakeFalModelsResponse:
    """为 fal 模型列表适配器提供最小 httpx.Response 兼容对象。"""

    def __init__(self, data, status_code=200):
        # 修改原因：models_adapter 只依赖 response.json 和 raise_for_status。
        # 修改方式：保存分页响应体和状态码，按需抛出 HTTP 错误。
        # 目的：让分页模型测试不访问真实 fal.ai 服务。
        self._data = data
        self.status_code = status_code

    def json(self):
        # 修改原因：fal 模型列表适配器按 httpx.Response.json 读取响应。
        # 修改方式：直接返回测试构造的 Python 字典。
        # 目的：固定 models API 的 items/page/pages 字段解析行为。
        return self._data

    def raise_for_status(self):
        # 修改原因：真实 httpx 会在非 2xx 状态下抛出异常，假响应也要保留该语义。
        # 修改方式：仅当状态码大于等于 400 时抛 RuntimeError。
        # 目的：避免测试替身掩盖适配器对上游错误的处理。
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeFalModelsClient:
    """记录 fal 模型列表分页请求，并返回预置分页数据。"""

    def __init__(self):
        # 修改原因：fetch_fal_models 需要按 fal.ai 实测 page 参数拉取所有分页。
        # 修改方式：按 URL 中的 page=2 简单分流，并记录请求参数。
        # 目的：验证实现没有只取第一页，也没有按状态或类型过滤模型。
        self.calls = []
        self.pages = {
            1: {
                "items": [
                    {"id": "fal-ai/nano-banana-2/edit", "status": "public"},
                    {"id": "fal-ai/deprecated-model", "deprecated": True},
                ],
                "page": 1,
                "size": 1000,
                "pages": 2,
                "total": 3,
            },
            2: {
                "items": [
                    {"id": "fal-ai/removed-model", "removed": True},
                ],
                "page": 2,
                "size": 1000,
                "pages": 2,
                "total": 3,
            },
        }

    async def get(self, url, headers=None, timeout=None):
        # 修改原因：测试需要观察 headers、timeout 和分页 URL。
        # 修改方式：保存调用记录，并按 page 查询参数返回相应分页。
        # 目的：确认适配器使用 fal.ai/api/models 且全量翻页。
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        page = 2 if "page=2" in url else 1
        return _FakeFalModelsResponse(self.pages[page])


def test_fal_register_exposes_models_adapter():
    # 修改原因：新增 fetch_fal_models 后必须挂到渠道注册表，否则管理端无法调用。
    # 修改方式：读取已注册 fal 渠道，断言 models_adapter 指向新增函数。
    # 目的：防止后续注册参数调整时遗漏模型列表适配器。
    from core.channels import get_channel
    from core.channels.fal_channel import fetch_fal_models

    channel = get_channel("fal")

    assert channel.models_adapter is fetch_fal_models


def test_get_fal_payload_extracts_prompt_images_and_native_fields():
    # 修改原因：fal 原生接口需要 prompt、图片 URL 和原生参数，不接受 Chat Completions 字段。
    # 修改方式：构造带文本、图片和 extra 字段的 RequestModel 后直接调用 request adapter。
    # 目的：确认转换结果使用 Key 鉴权，且不会把 stream、temperature 等字段传给 fal。
    from core.channels.fal_channel import get_fal_payload

    request = RequestModel(
        model="fal-ai/flux-2-pro",
        messages=[
            {"role": "system", "content": "ignore"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "create a small cat"},
                    {"type": "image_url", "image_url": {"url": "https://img.example/cat.png"}},
                ],
            },
        ],
        stream=True,
        temperature=0.8,
        image_size="square_hd",
        output_format="png",
    )
    provider = {"model": ["fal-ai/flux-2-pro"], "base_url": "https://fal.run"}

    url, headers, payload = asyncio.run(get_fal_payload(request, "fal", provider, api_key="fal-key"))

    assert url == "https://fal.run/fal-ai/flux-2-pro"
    assert headers["Authorization"] == "Key fal-key"
    assert payload["prompt"] == "create a small cat"
    assert payload["image_url"] == "https://img.example/cat.png"
    assert payload["image_size"] == "square_hd"
    assert payload["output_format"] == "png"
    assert "messages" not in payload
    assert "stream" not in payload
    assert "temperature" not in payload


def test_get_fal_payload_uses_queue_mode_from_provider_preference():
    # 修改原因：视频、音频和 3D 模型需要走 fal 队列端点，且 provider 偏好应优先于模型名判断。
    # 修改方式：分别覆盖强制 queue 和强制 sync 两种配置。
    # 目的：保证 fal_mode 不会被默认关键词规则覆盖。
    from core.channels.fal_channel import get_fal_payload

    queue_request = RequestModel(
        model="fal-ai/flux-2-pro",
        messages=[{"role": "user", "content": "queue please"}],
    )
    queue_provider = {
        "model": ["fal-ai/flux-2-pro"],
        "preferences": {"fal_mode": "queue"},
    }
    queue_url, _, _ = asyncio.run(get_fal_payload(queue_request, "fal", queue_provider, api_key="fal-key"))

    sync_request = RequestModel(
        model="fal-ai/kling-video-test",
        messages=[{"role": "user", "content": "sync please"}],
    )
    sync_provider = {
        "model": ["fal-ai/kling-video-test"],
        "preferences": {"fal_mode": "sync"},
    }
    sync_url, _, _ = asyncio.run(get_fal_payload(sync_request, "fal", sync_provider, api_key="fal-key"))

    assert queue_url == "https://queue.fal.run/fal-ai/flux-2-pro"
    assert sync_url == "https://fal.run/fal-ai/kling-video-test"


def test_format_fal_result_returns_markdown_content():
    # 修改原因：fal 响应必须直接拼成 markdown 文本，不能返回结构化 content items。
    # 修改方式：构造同时包含图片、视频、音频、3D、seed 和 revised prompt 的响应。
    # 目的：固定下游客户端实际收到的 Chat Completions content 字符串。
    from core.channels.fal_channel import _format_fal_result

    content = _format_fal_result(
        {
            "images": [{"url": "https://cdn.example/a.png"}],
            "video": {"url": "https://cdn.example/a.mp4"},
            "audio": {"url": "https://cdn.example/a.wav"},
            "model_glb": {"url": "https://cdn.example/a.glb"},
            "seed": 123,
            "prompt": "revised prompt",
        },
        {"prompt": "original prompt"},
    )

    assert "![image](https://cdn.example/a.png)" in content
    assert "[🎬 视频](https://cdn.example/a.mp4)" in content
    assert "[🔊 音频](https://cdn.example/a.wav)" in content
    assert "[🧊 3D模型](https://cdn.example/a.glb)" in content
    assert "*seed: 123*" in content
    assert "*Revised prompt: revised prompt*" in content


def test_fetch_fal_stream_supports_sync_mode_as_sse():
    # 修改原因：客户端即使请求 stream=true，同步 fal 图片端点仍需转换成 SSE。
    # 修改方式：用假客户端返回同步图片结果，并收集 stream adapter 输出。
    # 目的：确认同步模式也会发送 role、markdown 图片、stop 和 DONE。
    from core.channels.fal_channel import fetch_fal_stream

    client = _FakeFalClient()
    chunks = asyncio.run(_collect_async_chunks(fetch_fal_stream(
        client,
        "https://fal.run/fal-ai/flux-2-pro",
        {"Authorization": "Key fal-key"},
        {"prompt": "sync image"},
        "fal-ai/flux-2-pro",
        30,
    )))

    assert client.post_urls == ["https://fal.run/fal-ai/flux-2-pro"]
    assert any("https://cdn.example/sync.png" in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


def test_fetch_fal_stream_polls_queue_and_returns_final_markdown():
    # 修改原因：队列模式是视频、音频和 3D 生成的关键路径，需要轮询完成后再输出结果。
    # 修改方式：用假客户端模拟 request_id、COMPLETED 状态和最终视频结果。
    # 目的：验证 stream adapter 使用 queue.fal.run，并以 markdown 内容结束 SSE。
    from core.channels.fal_channel import fetch_fal_stream

    client = _FakeFalClient()
    chunks = asyncio.run(_collect_async_chunks(fetch_fal_stream(
        client,
        "https://queue.fal.run/fal-ai/kling-video-test",
        {"Authorization": "Key fal-key"},
        {"prompt": "queue video"},
        "fal-ai/kling-video-test",
        30,
    )))

    assert client.post_urls == ["https://queue.fal.run/fal-ai/kling-video-test"]
    assert client.get_urls == [
        "https://queue.fal.run/fal-ai/kling-video-test/requests/req_123/status?logs=1",
        "https://queue.fal.run/fal-ai/kling-video-test/requests/req_123",
    ]
    assert any("https://cdn.example/video.mp4" in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


async def _collect_async_chunks(generator):
    # 修改原因：stream adapter 返回异步生成器，普通断言需要先收集完整输出。
    # 修改方式：逐项遍历并返回列表。
    # 目的：让测试能检查 SSE 顺序和最终 DONE 标记。
    return [chunk async for chunk in generator]
