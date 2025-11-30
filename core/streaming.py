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
        async for chunk in self.body_iterator:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")

            # 音频流不解析 usage，直接透传
            if self.current_info.get("endpoint", "").endswith("/v1/audio/speech"):
                yield chunk
                continue

            line = chunk.decode("utf-8")
            if self.debug:
                logger.info(line.encode("utf-8").decode("unicode_escape"))

            if line.startswith("data:"):
                line = line.lstrip("data: ")

            if not line.startswith("[DONE]") and not line.startswith("OK") and not line.startswith(":"):
                try:
                    resp = await asyncio.to_thread(json.loads, line)
                    input_tokens = safe_get(resp, "message", "usage", "input_tokens", default=0)
                    input_tokens = safe_get(resp, "usage", "prompt_tokens", default=0)
                    output_tokens = safe_get(resp, "usage", "completion_tokens", default=0)
                    total_tokens = input_tokens + output_tokens

                    self.current_info["prompt_tokens"] = input_tokens
                    self.current_info["completion_tokens"] = output_tokens
                    self.current_info["total_tokens"] = total_tokens
                except Exception as e:
                    logger.error(f"Error parsing streaming response: {str(e)}, line: {repr(line)}")
                    # 出错时照样把原始 chunk 透传出去
            yield chunk

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if hasattr(self.body_iterator, "aclose"):
                await self.body_iterator.aclose()