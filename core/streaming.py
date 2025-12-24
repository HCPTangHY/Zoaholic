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

    async def _logging_iterator(self):
        # 用于收集响应体的缓冲区（仅在配置了保留时间时使用）
        # response_chunks 用于收集返回给用户的响应（即经过转换后的）
        response_chunks = []
        max_response_size = 100 * 1024  # 100KB
        total_response_size = 0
        should_save_response = self.current_info.get("raw_data_expires_at") is not None
        content_start_recorded = False  # 标记是否已记录正文开始时间
        
        async for chunk in self.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")

            # 收集响应体（限制大小）
            if should_save_response and total_response_size < max_response_size:
                response_chunks.append(chunk)
                total_response_size += len(chunk)

            # 音频流不解析 usage，直接透传
            if self.current_info.get("endpoint", "").endswith("/v1/audio/speech"):
                yield chunk
                continue

            # 使用 errors="replace" 避免解码错误导致流终止
            chunk_text = chunk.decode("utf-8", errors="replace")
            if self.debug:
                logger.info(chunk_text.encode("utf-8").decode("unicode_escape"))

            # 按行分割处理，一个 chunk 可能包含多个 SSE 事件
            lines = chunk_text.split("\n")
            for line in lines:
                line = line.strip()
                
                # 跳过空行和注释行
                if not line or line.startswith(":"):
                    continue

                if line.startswith("data:"):
                    line = line[5:].strip()  # 移除 "data:" 前缀（5个字符）

                # 跳过特殊标记和空行
                if not line or line.startswith("[DONE]") or line.startswith("OK"):
                    continue

                # 尝试解析 JSON
                try:
                    resp = await asyncio.to_thread(json.loads, line)
                    
                    # 检测正文开始时间（首个非空 content，不含 reasoning_content）
                    # 注意：如果是在方言转换后，choices 结构可能已变，这里优先支持 OpenAI 风格探测
                    if not content_start_recorded:
                        choices = resp.get("choices")
                        if choices and isinstance(choices, list) and len(choices) > 0:
                            content = safe_get(choices[0], "delta", "content", default=None)
                            if content and content.strip():
                                self.current_info["content_start_time"] = time() - self.current_info.get("start_time", time())
                                content_start_recorded = True
                    
                    # 使用方言注册的 usage 解析方法
                    from core.dialects.registry import get_dialect
                    
                    # 优先使用显式指定的方言，否则从 current_info 获取，默认 openai
                    d_id = self.dialect_id or self.current_info.get("dialect_id") or "openai"
                    dialect = get_dialect(d_id)
                    
                    usage_info = None
                    if dialect and dialect.parse_usage:
                        usage_info = dialect.parse_usage(resp)
                    
                    # 如果当前方言未解析出 usage 且不是 openai，尝试用 openai 格式保底（处理 Canonical 转换后的情况）
                    if not usage_info and d_id != "openai":
                        o_dialect = get_dialect("openai")
                        if o_dialect and o_dialect.parse_usage:
                            usage_info = o_dialect.parse_usage(resp)

                    if usage_info:
                        self.current_info["prompt_tokens"] = usage_info.get("prompt_tokens", 0)
                        self.current_info["completion_tokens"] = usage_info.get("completion_tokens", 0)
                        self.current_info["total_tokens"] = usage_info.get("total_tokens", 0)
                except Exception as e:
                    # 仅在调试模式下记录解析错误，避免正常运行时的噪音
                    if self.debug:
                        logger.error(f"Error parsing streaming response: {str(e)}, line: {repr(line)}")
                    # 出错时继续处理下一行
            
            # 透传原始 chunk
            yield chunk
        
        # 保存返回给用户的响应体（使用深度截断，保留结构同时限制大小）
        if should_save_response and response_chunks:
            try:
                response_body = b"".join(response_chunks)
                self.current_info["response_body"] = truncate_for_logging(response_body)
            except Exception as e:
                logger.error(f"Error saving response body: {str(e)}")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if hasattr(self.body_iterator, "aclose"):
                await self.body_iterator.aclose()