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
    ):
        super().__init__(content=None, status_code=status_code, headers=headers, media_type=media_type)
        self.body_iterator = content
        self._closed = False
        self.current_info = current_info or {}
        self.app = app
        self.debug = debug

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
        response_chunks = []
        max_response_size = 100 * 1024  # 100KB
        total_response_size = 0
        should_save_response = self.current_info.get("raw_data_expires_at") is not None
        
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

            chunk_text = chunk.decode("utf-8")
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
                    # Claude API 的 usage 字段
                    input_tokens = safe_get(resp, "message", "usage", "input_tokens", default=0)
                    # OpenAI 兼容的 usage 字段
                    if not input_tokens:
                        input_tokens = safe_get(resp, "usage", "prompt_tokens", default=0)
                    output_tokens = safe_get(resp, "usage", "completion_tokens", default=0)
                    total_tokens = input_tokens + output_tokens

                    if total_tokens > 0:
                        self.current_info["prompt_tokens"] = input_tokens
                        self.current_info["completion_tokens"] = output_tokens
                        self.current_info["total_tokens"] = total_tokens
                except Exception as e:
                    # 仅在调试模式下记录解析错误，避免正常运行时的噪音
                    if self.debug:
                        logger.error(f"Error parsing streaming response: {str(e)}, line: {repr(line)}")
                    # 出错时继续处理下一行
            
            # 透传原始 chunk
            yield chunk
        
        # 保存响应体
        if should_save_response and response_chunks:
            try:
                response_body = b"".join(response_chunks).decode("utf-8", errors="replace")
                if total_response_size >= max_response_size:
                    response_body += f"\n[Truncated, total size: {total_response_size} bytes]"
                self.current_info["response_body"] = response_body
            except Exception as e:
                logger.error(f"Error saving response body: {str(e)}")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if hasattr(self.body_iterator, "aclose"):
                await self.body_iterator.aclose()