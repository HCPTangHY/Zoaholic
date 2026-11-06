"""
流式解析辅助模块。

提供基于 bytes 的按行解析器，减少字符串累加复制。
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterable, AsyncIterator

from anyio import CancelScope


async def close_async_iterator(iterator, pending_task=None):
    """Cancel a pending read before closing its iterator, including in ASGI cancel scopes."""
    with CancelScope(shield=True):
        try:
            if pending_task is not None:
                if not pending_task.done():
                    pending_task.cancel()
                await asyncio.gather(pending_task, return_exceptions=True)
        finally:
            if hasattr(iterator, "aclose"):
                await iterator.aclose()


class OwnedAsyncIterator:
    """Keep cleanup available even before a response generator's first iteration."""

    def __init__(self, iterator, source, pending_task=None):
        self.iterator = iterator
        self.source = source
        self.pending_task = pending_task

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await anext(self.iterator)

    async def aclose(self):
        try:
            await close_async_iterator(self.iterator)
        finally:
            await close_async_iterator(self.source, self.pending_task)
            self.pending_task = None


async def aiter_decoded_lines(
    source: AsyncIterable[bytes | bytearray | str],
    *,
    delimiter: bytes = b"\n",
    encoding: str = "utf-8",
    errors: str = "replace",
    cooperative_yield_threshold: int = 64 * 1024,
) -> AsyncIterator[str]:
    """把异步字节流按分隔符切分为文本行。

    说明：
    - 使用 bytearray 缓冲，减少 `buffer += chunk` 的字符串复制。
    - 只有在获得完整行后才解码。
    - 剩余尾部会在结束时作为最后一行返回。
    """
    if not delimiter:
        raise ValueError("delimiter must not be empty")

    buffer = bytearray()
    delimiter_len = len(delimiter)

    async for chunk in source:
        if not chunk:
            continue
        if isinstance(chunk, str):
            chunk = chunk.encode(encoding)

        buffer.extend(chunk)

        while True:
            idx = buffer.find(delimiter)
            if idx < 0:
                break
            line_bytes = bytes(buffer[:idx])
            del buffer[:idx + delimiter_len]
            yield line_bytes.decode(encoding, errors=errors)

        if len(buffer) > cooperative_yield_threshold:
            await asyncio.sleep(0)

    if buffer:
        yield bytes(buffer).decode(encoding, errors=errors)


async def aiter_line_aligned_text(source: AsyncIterable[str]) -> AsyncIterator[str]:
    """把文本流重新切分到行边界，原样保留换行符与空行。

    说明：
    - 每次产出以 "\\n" 结尾的若干完整行，拼接结果与输入逐字节一致。
    - 不含换行的分片先缓存，等到下一个换行再与之合并产出；结束时产出剩余尾部。
    - 用列表缓存未完成的行，超长单行（如内联 Base64 图片）也不会反复复制字符串。
    """
    tail: list[str] = []
    async for chunk in source:
        if not chunk:
            continue
        cut = chunk.rfind("\n")
        if cut < 0:
            tail.append(chunk)
            continue
        tail.append(chunk[:cut + 1])
        yield "".join(tail)
        rest = chunk[cut + 1:]
        tail = [rest] if rest else []

    if tail:
        yield "".join(tail)
