"""
Streaming response helpers.

提供带统计和错误处理的流式响应包装器。
"""

import json
import asyncio
from time import time

from starlette.responses import Response
from starlette.types import Scope, Receive, Send

from core.log_config import logger
from core.stats import update_stats
from core.utils import truncate_for_logging
from utils import safe_get


class LoggingStreamingResponse(Response):
    """
    包装底层流式响应：
    - 透传 chunk 给客户端
    - 解析 usage 字段，填充 current_info 中的 token 统计
    - 在完成后调用 update_stats 写入数据库
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
        self.current_info = current_info or {}
        self.app = app
        self.debug = debug
        self.dialect_id = dialect_id or self.current_info.get("dialect_id")

        # Remove Content-Length header if it exists
        if "content-length" in self.headers:
            del self.headers["content-length"]
        # Set Transfer-Encoding to chunked
        self.headers["transfer-encoding"] = "chunked"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )

        try:
            async for chunk in self._logging_iterator():
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    }
                )
        except Exception as e:
            # 记录异常但不重新抛出，避免"Task exception was never retrieved"
            logger.error(f"Error in streaming response: {type(e).__name__}: {str(e)}")
            if self.debug:
                import traceback

                traceback.print_exc()
            # 发送错误消息给客户端（如果可能）
            try:
                error_data = json.dumps({"error": f"Streaming error: {str(e)}"})
                await send(
                    {
                        "type": "http.response.body",
                        "body": f"data: {error_data}\n\n".encode("utf-8"),
                        "more_body": True,
                    }
                )
            except Exception as send_err:
                logger.error(f"Error sending error message: {str(send_err)}")
        finally:
            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                }
            )
            if hasattr(self.body_iterator, "aclose") and not self._closed:
                await self.body_iterator.aclose()
                self._closed = True

            # 记录处理时间并写入统计
            if "start_time" in self.current_info:
                process_time = time() - self.current_info["start_time"]
                self.current_info["process_time"] = process_time
            try:
                await update_stats(self.current_info, app=self.app)
            except Exception as e:
                logger.error(f"Error updating stats in LoggingStreamingResponse: {str(e)}")

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

        # 当前方言未解析出 usage 且不是 openai 时，用 openai 格式保底
        if not usage_info and d_id != "openai":
            o_dialect = get_dialect("openai")
            if o_dialect and o_dialect.parse_usage:
                usage_info = o_dialect.parse_usage(resp)

        if usage_info:
            for _usage_key in ("prompt_tokens", "completion_tokens"):
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
        # 用于收集响应体的缓冲区（仅在配置了保留时间时使用）
        # response_chunks 用于收集返回给用户的响应（即经过转换后的）
        response_chunks = []
        max_response_size = 100 * 1024  # 100KB
        total_response_size = 0
        should_save_response = self.current_info.get("raw_data_expires_at") is not None
        adapter_metrics_managed = bool(self.current_info.get("adapter_metrics_managed"))
        content_start_recorded = False  # 标记是否已记录正文开始时间
        # 跨 chunk 行缓冲：上游 HTTP chunk 边界与 SSE 行边界不一定对齐，
        # 一个 data: 行可能被拆到相邻两个 chunk 中。
        # 保留上一个 chunk 末尾的不完整行，拼接到下一个 chunk 开头。
        _line_buffer = ""
        
        async for chunk in self.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")

            # 收集响应体（限制大小）
            if should_save_response and total_response_size < max_response_size:
                response_chunks.append(chunk)
                total_response_size += len(chunk)

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
        
        # 处理 _line_buffer 中的残留数据
        # 流的最后一个 chunk 可能不以换行结尾，此时最后一行 data 会留在缓冲区中
        if _line_buffer:
            try:
                self._try_parse_line(_line_buffer, content_start_recorded)
            except Exception as e:
                if self.debug:
                    logger.error(f"Error parsing remaining buffer: {str(e)}, line: {repr(_line_buffer)}")

        # 保存返回给用户的响应体（使用深度截断，保留结构同时限制大小）
        # 使用 asyncio.to_thread 避免大响应体阻塞事件循环
        if should_save_response and response_chunks:
            try:
                response_body = b"".join(response_chunks)
                self.current_info["response_body"] = await asyncio.to_thread(truncate_for_logging, response_body)
            except Exception as e:
                logger.error(f"Error saving response body: {str(e)}")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if hasattr(self.body_iterator, "aclose"):
                await self.body_iterator.aclose()
