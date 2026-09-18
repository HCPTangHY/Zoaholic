"""
Streaming response helpers.

提供带统计和错误处理的流式响应包装器。
"""

import json
import asyncio
import weakref
from time import time

from starlette.responses import Response
from starlette.types import Scope, Receive, Send

from core.log_config import logger
from core.stream_utils import close_async_iterator
from core.stream_errors import (
    StreamInspector, UpstreamStreamError, record_stream_failure, stream_error_from_exception,
)
from core.stats import enqueue_stats
from core.utils import truncate_for_logging
from utils import safe_get


class LoggingStreamingResponse(Response):
    """
    包装底层流式响应：
    - 透传 chunk 给客户端
    - 解析 usage 字段，填充 current_info 中的 token 统计
    - 在完成后调用 enqueue_stats 入队，由后台 consumer 批量写入数据库
    """

    def __init__(
        self,
        content,
        status_code=200,
        headers=None,
        media_type=None,
        current_info=None,
        app=None,
        debug=False,
        dialect_id=None,
    ):
        super().__init__(content=None, status_code=status_code, headers=headers, media_type=media_type)
        self.body_iterator = content
        self._closed = False
        self.current_info = current_info if current_info is not None else {}
        # 修改原因：流式 Response 持有 FastAPI app 强引用会把 app.state 上的注册表一并留在引用链中。
        # 修改方式：仅保存 weakref.ref，使用时再解引用，避免 Response → app → state 的循环引用。
        # 目的：让每个流式请求完成后可以更快释放响应对象和相关请求上下文。
        self.app = weakref.ref(app) if (app is not None and not isinstance(app, weakref.ReferenceType)) else app
        self.debug = debug
        self.dialect_id = dialect_id or self.current_info.get("dialect_id")

        # Remove Content-Length header if it exists
        if "content-length" in self.headers:
            del self.headers["content-length"]
        # Set Transfer-Encoding to chunked
        self.headers["transfer-encoding"] = "chunked"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        current_info = self.current_info
        logging_iterator = self._logging_iterator()
        response_chunks = []
        remaining = 100 * 1024
        should_save = current_info.get("raw_data_expires_at") is not None
        try:
            await send({
                "type": "http.response.start", "status": self.status_code,
                "headers": self.raw_headers,
            })
            # 心跳也会提交 HTTP 200；内部预读和非流式组装则不能设置此标记。
            current_info["_stream_committed"] = True
            async for chunk in logging_iterator:
                await send({
                    "type": "http.response.body", "body": chunk, "more_body": True,
                })
                # 正常帧和错误帧走同一记录入口，只保存 send 成功返回的字节。
                if should_save and remaining:
                    prefix = chunk[:remaining]
                    response_chunks.append(prefix)
                    remaining -= len(prefix)
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            if not current_info.get("_stream_error"):
                current_info.update(success=False, status_code=499)
            raise
        except Exception as exc:
            # 上游异常由迭代器转换为错误帧；发送失败不再尝试向断开的客户端发送错误。
            if not current_info.get("_stream_error"):
                current_info.update(success=False, status_code=499)
            logger.warning("Error sending streaming response: %s", type(exc).__name__)
        finally:
            try:
                try:
                    await close_async_iterator(logging_iterator)
                finally:
                    await self.close()
            finally:
                try:
                    app = self.app() if self.app else None
                    if should_save and response_chunks:
                        current_info["response_body"] = truncate_for_logging(b"".join(response_chunks))
                    if "start_time" in current_info:
                        current_info["process_time"] = time() - current_info["start_time"]
                    # 渠道统计必须等待真实流结束，不能在创建 Response 时提前记成功。
                    channel_stats = current_info.pop("_channel_stats_call", None)
                    if channel_stats:
                        from core.handler import _fire_and_forget_channel_stats
                        func, args, kwargs = channel_stats
                        _fire_and_forget_channel_stats(func, *args,
                                                      success=bool(current_info.get("success")), **kwargs)
                    enqueue_stats(current_info, app=app)
                except Exception as exc:
                    logger.error("Error enqueueing streaming stats: %s", type(exc).__name__)
                finally:
                    self.current_info = None
                    self.body_iterator = None

    async def _render_stream_error(self, error):
        """协议字段由入口方言生成；核心只提供统一错误对象。"""
        from core.dialects.registry import get_dialect

        payload = {"error": {"message": error["message"], "type": error["type"],
                             "param": None, "code": error.get("code")},
                   "status_code": error["status_code"]}
        text = json.dumps(payload, ensure_ascii=False)
        if self.media_type != "text/event-stream":
            return text
        canonical = f"data: {text}\n\n"
        dialect = get_dialect(self.dialect_id or "openai")
        if dialect:
            render = dialect.render_stream_factory() if dialect.render_stream_factory else dialect.render_stream
            if render:
                return await render(canonical) or canonical
        return canonical

    async def _iterate_with_errors(self):
        """先处理实际 Key，再交付原生错误帧或按方言生成的异常帧。"""
        inspector = StreamInspector()
        try:
            async for chunk in self.body_iterator:
                inspector.feed(chunk)
                if inspector.has_output:
                    self.current_info["_stream_has_output"] = True
                if inspector.error:
                    await record_stream_failure(self.current_info, inspector.error)
                yield chunk
            inspector.finish()
            if inspector.error:
                await record_stream_failure(self.current_info, inspector.error)
            elif self.current_info.get("_stream_error"):
                # 转换器或插件过滤了错误帧时，也不能以正常空流结束。
                raise UpstreamStreamError(self.current_info["_stream_error"])
            elif (self.media_type == "text/event-stream" and not inspector.terminal
                  and not self.current_info.get("_stream_has_output")):
                raise RuntimeError("Upstream stream ended before output")
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as exc:
            error = self.current_info.get("_stream_error") or stream_error_from_exception(exc)
            await record_stream_failure(self.current_info, error)
            logger.error("Error in streaming response: %s: %s", type(exc).__name__, error["message"])
            yield await self._render_stream_error(error)
        finally:
            await close_async_iterator(self.body_iterator)

    def _try_extract_usage(self, resp: dict) -> None:
        """从已解析的 JSON 对象中提取 usage 并合并到 current_info。

        合并策略：仅更新非零值，避免后到的事件覆盖先到的非零值。
        例如 Claude 流式响应将 input_tokens 和 output_tokens 分散在不同事件中。
        """
        from core.dialects.registry import get_dialect

        d_id = self.dialect_id or self.current_info.get("dialect_id") or "openai"
        dialect = get_dialect(d_id)

        usage_info = None
        if dialect and dialect.parse_usage:
            usage_info = dialect.parse_usage(resp)

        # 当前方言未解析出 usage 且不是 openai 时，用 openai 格式保底。
        if not usage_info and d_id != "openai":
            o_dialect = get_dialect("openai")
            if o_dialect and o_dialect.parse_usage:
                usage_info = o_dialect.parse_usage(resp)

        if not usage_info:
            # 透传响应可能是任意原生协议；最后用宽松 parser 覆盖缓存字段，避免 current_info 漏记。
            from core.dialects.passthrough import parse_passthrough_usage
            usage_info = parse_passthrough_usage(resp)

        if usage_info:
            # usage 解析同时覆盖普通 token 与 Prompt Caching 字段，保证透传流式响应也能入库缓存统计。
            for _usage_key in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_creation_tokens"):
                new_val = usage_info.get(_usage_key, 0)
                if new_val > 0:
                    self.current_info[_usage_key] = new_val
            # total_tokens 始终重算，确保一致性
            self.current_info["total_tokens"] = (
                self.current_info.get("prompt_tokens", 0)
                + self.current_info.get("completion_tokens", 0)
            )

    def _try_parse_line(self, line: str, content_start_recorded: bool) -> bool:
        """尝试解析单行 SSE 数据，提取 usage 和 content_start_time。

        Returns:
            更新后的 content_start_recorded 标志
        """
        line = line.strip()

        # 跳过空行、注释行和 SSE event 类型行
        if not line or line.startswith(":") or line.startswith("event:"):
            return content_start_recorded

        if line.startswith("data:"):
            line = line[5:].strip()

        # 跳过特殊标记和空行
        if not line or line.startswith("[DONE]") or line.startswith("OK"):
            return content_start_recorded

        # 尝试解析 JSON —— 同步调用 json.loads 以避免 await（此方法非 async）
        # 由于外层已在 asyncio 中，这里直接调用；JSON 解析通常足够快
        try:
            resp = json.loads(line)
        except Exception:
            return content_start_recorded

        # 检测正文开始时间
        if not content_start_recorded:
            choices = resp.get("choices")
            if choices and isinstance(choices, list) and len(choices) > 0:
                content = safe_get(choices[0], "delta", "content", default=None)
                if content and content.strip():
                    self.current_info["content_start_time"] = time() - self.current_info.get("start_time", time())
                    content_start_recorded = True

        # 提取 usage
        self._try_extract_usage(resp)

        return content_start_recorded

    async def _logging_iterator(self):
        # 非流式 JSON 可能被分块，保留有界缓冲用于 usage 解析，不依赖原始日志开关。
        response_chunks = []
        max_response_size = 100 * 1024
        total_response_size = 0
        adapter_metrics_managed = bool(self.current_info.get("adapter_metrics_managed"))
        content_start_recorded = False  # 标记是否已记录正文开始时间
        # 跨 chunk 行缓冲：上游 HTTP chunk 边界与 SSE 行边界不一定对齐，
        # 一个 data: 行可能被拆到相邻两个 chunk 中。
        # 保留上一个 chunk 末尾的不完整行，拼接到下一个 chunk 开头。
        _line_buffer = ""

        iterator = self._iterate_with_errors()
        try:
            async for chunk in iterator:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")

                if self.media_type != "text/event-stream" and total_response_size < max_response_size:
                    prefix = chunk[:max_response_size - total_response_size]
                    response_chunks.append(prefix)
                    total_response_size += len(prefix)

                # 若 usage / content_start_time 已由适配器直接管理，
                # 这里不再对已经转换过的下游响应做二次 JSON 解析。
                if adapter_metrics_managed:
                    yield chunk
                    continue

                # 音频流不解析 usage，直接透传
                if self.current_info.get("endpoint", "").endswith("/v1/audio/speech"):
                    yield chunk
                    continue

                # 使用 errors="replace" 避免解码错误导致流终止
                chunk_text = chunk.decode("utf-8", errors="replace")
                if self.debug:
                    logger.info(chunk_text.encode("utf-8").decode("unicode_escape"))

                # 拼接上一个 chunk 的残留行
                chunk_text = _line_buffer + chunk_text
                _line_buffer = ""

                # 按行分割；最后一个元素可能是不完整行，需要缓冲
                lines = chunk_text.split("\n")
                # 如果 chunk 不以换行结尾，末尾元素是不完整行，留到下个 chunk
                if not chunk_text.endswith("\n"):
                    _line_buffer = lines.pop()

                for line in lines:
                    try:
                        content_start_recorded = self._try_parse_line(line, content_start_recorded)
                    except Exception as e:
                        if self.debug:
                            logger.error(f"Error parsing streaming response: {str(e)}, line: {repr(line)}")

                # 透传原始 chunk
                yield chunk

        finally:
            await close_async_iterator(iterator)

        # 处理 _line_buffer 中的残留数据
        # 流的最后一个 chunk 可能不以换行结尾，此时最后一行 data 会留在缓冲区中
        if _line_buffer:
            try:
                self._try_parse_line(_line_buffer, content_start_recorded)
            except Exception as e:
                if self.debug:
                    logger.error(f"Error parsing remaining buffer: {str(e)}, line: {repr(_line_buffer)}")

        # 非 SSE 响应（如 Gemini 非流式透传）的 usage 提取：
        # _try_parse_line 只能解析 SSE 格式（按行 data: {json}），
        # 纯 JSON 响应按行切分后每行都不是完整 JSON，导致 usage 漏采。
        # 流结束后如果 completion_tokens 仍为 0，尝试把完整响应体当 JSON 解析。
        if self.current_info.get("completion_tokens", 0) == 0 and response_chunks:
            try:
                full_body = b"".join(response_chunks).decode("utf-8", errors="replace")
                full_resp = json.loads(full_body)
                if isinstance(full_resp, dict):
                    self._try_extract_usage(full_resp)
            except Exception:
                pass

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            iterator = self.body_iterator
            # 修改原因：__call__ 结束后会把 body_iterator 清空，close 可能在清理后被再次调用。
            # 修改方式：先取局部 iterator，并在存在 aclose 方法时才关闭。
            # 目的：保持 close 幂等，避免清理引用后再次关闭触发 AttributeError。
            if iterator is not None and hasattr(iterator, "aclose"):
                await close_async_iterator(iterator)
