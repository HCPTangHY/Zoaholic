"""Request-scoped Chat Completions -> Responses output conversion.

Native Responses passthrough must bypass this adapter. Conversion happens before
LoggingStreamingResponse consumes the iterator, so logs and usage match the wire.
"""
from __future__ import annotations

import codecs
import copy
import json
import logging
import time
import uuid

from core.stream_utils import close_async_iterator
from core.stream_errors import UpstreamStreamError

logger = logging.getLogger(__name__)


def new_id(prefix: str) -> str:
    return prefix + uuid.uuid4().hex


class ResponsesStreamRenderer:
    def __init__(self, model: str):
        self.response = {
            "id": new_id("resp_"), "object": "response", "created_at": int(time.time()),
            "model": model, "status": "in_progress", "output": [],
            "error": None, "incomplete_details": None, "usage": None,
        }
        self.sequence = 0
        self.started = False
        self.terminal = False
        self.finish_reason = None
        self.parts = {}
        self.tools = {}

    def event(self, kind: str, **fields) -> str:
        data = {"type": kind, "sequence_number": self.sequence, **fields}
        self.sequence += 1
        return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def start(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        return [self.event("response.created", response=self.response),
                self.event("response.in_progress", response=self.response)]

    def add_item(self, item: dict) -> tuple[int, list[str]]:
        index = len(self.response["output"])
        self.response["output"].append(item)
        return index, [self.event("response.output_item.added", output_index=index,
                                  item=copy.deepcopy(item))]

    def text(self, delta: str, kind="output_text") -> list[str]:
        if not delta:
            return []
        reasoning = kind == "reasoning_summary_text"
        key = "refusal" if kind == "refusal" else "text"
        container = "summary" if reasoning else "content"
        index_key = "summary_index" if reasoning else "content_index"
        part_event = "reasoning_summary_part" if reasoning else "content_part"
        events = []
        index = self.parts.get(kind)
        if index is None:
            if reasoning:
                item = {"id": new_id("rs_"), "type": "reasoning", "summary": []}
                part = {"type": "summary_text", "text": ""}
            else:
                item = {"id": new_id("msg_"), "type": "message", "role": "assistant",
                        "status": "in_progress", "content": []}
                part = ({"type": "refusal", "refusal": ""} if kind == "refusal" else
                        {"type": "output_text", "text": "", "annotations": []})
            index, added = self.add_item(item)
            self.parts[kind] = index
            events.extend(added)
            item[container].append(part)
            events.append(self.event(f"response.{part_event}.added", item_id=item["id"],
                                     output_index=index, **{index_key: 0}, part=part))
        item = self.response["output"][index]
        item[container][0][key] += delta
        events.append(self.event(f"response.{kind}.delta", item_id=item["id"],
                                 output_index=index, **{index_key: 0}, delta=delta))
        return events

    def feed(self, chunk: dict) -> list[str]:
        if self.terminal:
            return []
        if not isinstance(chunk, dict):
            raise ValueError("Expected a canonical response object")
        events = self.start()
        if chunk.get("error"):
            return events + self.finish(error=chunk["error"])
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            from .openai_responses import _responses_usage_from_canonical
            self.response["usage"] = _responses_usage_from_canonical(usage)
        for choice in chunk.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                events.extend(self.text(content))
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") in ("text", "output_text"):
                        events.extend(self.text(part.get("text", "")))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        image = part.get("image_url")
                        url = image.get("url", "") if isinstance(image, dict) else image
                        if url:
                            events.extend(self.text(f"![image]({url})"))
            events.extend(self.text(delta.get("reasoning_content") or "", "reasoning_summary_text"))
            events.extend(self.text(delta.get("refusal") or "", "refusal"))
            for tc in delta.get("tool_calls") or []:
                tool_index = tc.get("index", 0)
                fn = tc.get("function") or {}
                if tool_index not in self.tools:
                    item = {"id": new_id("fc_"), "type": "function_call", "status": "in_progress",
                            "call_id": tc.get("id") or new_id("call_"),
                            "name": fn.get("name") or "", "arguments": ""}
                    index, added = self.add_item(item)
                    self.tools[tool_index] = index
                    events.extend(added)
                else:
                    index = self.tools[tool_index]
                    item = self.response["output"][index]
                    if tc.get("id"):
                        item["call_id"] = tc["id"]
                    if fn.get("name"):
                        item["name"] += fn["name"]
                args = fn.get("arguments") or ""
                if args:
                    item["arguments"] += args
                    events.append(self.event("response.function_call_arguments.delta",
                                             item_id=item["id"], output_index=index, delta=args))
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def finish(self, *, error=None) -> list[str]:
        if self.terminal:
            return []
        events = self.start()
        self.terminal = True
        if not error and not self.finish_reason:
            error = {"code": "incomplete_stream", "message": "Upstream stream ended without a finish reason"}
        if error:
            if not isinstance(error, dict):
                error = {"code": "upstream_error", "message": str(error)}
            self.response.update(status="failed", error=error)
            return events + [self.event("response.failed", response=self.response)]
        incomplete = self.finish_reason in ("length", "content_filter")
        status = "incomplete" if incomplete else "completed"
        for index, item in enumerate(self.response["output"]):
            fields = {"item_id": item["id"], "output_index": index}
            if item["type"] == "function_call":
                events.append(self.event("response.function_call_arguments.done", **fields,
                                         name=item["name"], arguments=item["arguments"]))
                item["status"] = status
            elif item["type"] == "message":
                part = item["content"][0]
                key = "refusal" if part["type"] == "refusal" else "text"
                kind = "refusal" if key == "refusal" else "output_text"
                events.append(self.event(f"response.{kind}.done", **fields,
                                         content_index=0, **{key: part[key]}))
                events.append(self.event("response.content_part.done", **fields,
                                         content_index=0, part=part))
                item["status"] = status
            else:
                part = item["summary"][0]
                events.append(self.event("response.reasoning_summary_text.done", **fields,
                                         summary_index=0, text=part["text"]))
                events.append(self.event("response.reasoning_summary_part.done", **fields,
                                         summary_index=0, part=part))
            events.append(self.event("response.output_item.done", output_index=index, item=item))
        self.response["status"] = status
        if incomplete:
            self.response["incomplete_details"] = {"reason":
                "max_output_tokens" if self.finish_reason == "length" else "content_filter"}
        events.append(self.event(f"response.{status}", response=self.response))
        return events


async def render_responses_iterator(source, model: str, *, stream: bool):
    if not stream:
        from .openai_responses import render_responses_response
        try:
            async for value in source:
                if isinstance(value, (str, bytes)):
                    value = json.loads(value)
                yield json.dumps(await render_responses_response(value, model), ensure_ascii=False)
        finally:
            if hasattr(source, "aclose"):
                await close_async_iterator(source)
        return
    renderer = ResponsesStreamRenderer(model)
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""

    def frame_events(frame):
        data = "\n".join(line[5:].lstrip(" ") for line in frame.splitlines()
                         if line.startswith("data:"))
        if not data:
            return [frame + "\n\n"] if frame.startswith(":") else []
        if data == "[DONE]":
            return renderer.finish()
        return renderer.feed(json.loads(data))

    try:
        async for value in source:
            if isinstance(value, dict):
                for event in renderer.feed(value):
                    yield event
                continue
            buffer += decoder.decode(value) if isinstance(value, bytes) else value
            buffer = buffer.replace("\r\n", "\n")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                for event in frame_events(frame):
                    yield event
        buffer += decoder.decode(b"", final=True)
        if buffer.strip():
            for event in frame_events(buffer):
                yield event
        for event in renderer.finish():
            yield event
    except UpstreamStreamError as exc:
        # Preserve upstream status/code for LoggingStreamingResponse and key rules.
        # Do not turn a 429/503 into a generic conversion error.
        for event in renderer.finish(error=exc.error):
            yield event
    except Exception:
        logger.exception("Failed to convert upstream stream to Responses events")
        for event in renderer.finish(error={"code": "upstream_stream_error",
                                            "message": "Upstream stream failed or contained invalid data"}):
            yield event
    finally:
        if hasattr(source, "aclose"):
            await close_async_iterator(source)
