import asyncio
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.utils import generate_chunked_image_md, generate_no_stream_response
from utils import error_handling_wrapper


def _collect_sse_content(events: list[str]) -> str:
    parts: list[str] = []
    for event in events:
        assert event.startswith("data: ")
        payload = json.loads(event[6:].strip())
        choices = payload.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            parts.append(content)
    return "".join(parts)


def test_generate_chunked_image_md_reconstructs_expected_markdown():
    async def _run():
        events = []
        async for event in generate_chunked_image_md(
            "ABCDE",
            timestamp=1,
            model="test-model",
            chunk_size=12,
            mime_type="image/png",
        ):
            events.append(event)
        return events

    events = asyncio.run(_run())
    content = _collect_sse_content(events)
    assert content == "\n\n![image](data:image/png;base64,ABCDE)"


def test_generate_no_stream_response_return_dict_keeps_image_payload():
    payload = asyncio.run(
        generate_no_stream_response(
            timestamp=1,
            model="test-model",
            image_base64="ABCDEF",
            return_dict=True,
        )
    )

    assert payload["data"][0]["b64_json"] == "ABCDEF"
    assert "choices" not in payload


def test_error_handling_wrapper_non_stream_dict_returns_plain_json_text():
    async def _generator():
        yield {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }

    async def _run():
        wrapped, _ = await error_handling_wrapper(
            _generator(),
            channel_id="test",
            engine="openai",
            stream=False,
            error_triggers=[],
        )
        return await anext(wrapped)

    first_chunk = asyncio.run(_run())
    assert first_chunk.startswith("{")
    assert not first_chunk.startswith("data: ")
    parsed = json.loads(first_chunk)
    assert parsed["choices"][0]["message"]["content"] == "ok"
