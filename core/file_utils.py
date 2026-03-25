import asyncio
import base64
import mimetypes
from typing import Optional

import httpx
from fastapi import HTTPException


_shared_fetch_client: Optional[httpx.AsyncClient] = None
_shared_fetch_client_lock = asyncio.Lock()


def parse_data_uri(data_uri: str) -> tuple[str, str]:
    if not data_uri.startswith("data:"):
        raise ValueError("Invalid data URI")
    header, data = data_uri.split(",", 1)
    mime_type = header[5:].split(";")[0]
    return mime_type, data


def build_data_uri(mime_type: str, base64_data: str) -> str:
    return f"data:{mime_type};base64,{base64_data}"


def split_data_uri_prefix_and_data(data_uri_or_b64: str, default_mime_type: str = "image/png") -> tuple[str, str]:
    if isinstance(data_uri_or_b64, str) and data_uri_or_b64.startswith("data:") and "," in data_uri_or_b64:
        header, data = data_uri_or_b64.split(",", 1)
        return header + ",", data
    return f"data:{default_mime_type};base64,", data_uri_or_b64


def extract_base64_data(data_uri_or_b64: str) -> str:
    if isinstance(data_uri_or_b64, str) and data_uri_or_b64.startswith("data:"):
        _, data = parse_data_uri(data_uri_or_b64)
        return data
    return data_uri_or_b64


def guess_mime_type(filename: str = None, default="application/octet-stream") -> str:
    if filename:
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type:
            return mime_type
    return default


def _encode_bytes_to_base64_text(content: bytes) -> str:
    return base64.b64encode(content).decode("utf-8")


async def _get_shared_fetch_client() -> httpx.AsyncClient:
    global _shared_fetch_client
    if _shared_fetch_client is not None:
        return _shared_fetch_client

    async with _shared_fetch_client_lock:
        if _shared_fetch_client is not None:
            return _shared_fetch_client

        transport = httpx.AsyncHTTPTransport(http2=True, verify=False, retries=1)
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=10.0)
        _shared_fetch_client = httpx.AsyncClient(
            transport=transport,
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
        )
        return _shared_fetch_client


async def close_shared_fetch_client() -> None:
    global _shared_fetch_client
    async with _shared_fetch_client_lock:
        if _shared_fetch_client is not None:
            await _shared_fetch_client.aclose()
            _shared_fetch_client = None


async def fetch_url_content(url: str) -> tuple[bytes, str]:
    client = await _get_shared_fetch_client()
    try:
        response = await client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
        return response.content, content_type
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch file from url: {url}. Error: {str(e)}")


async def get_base64_file(file_url_or_data: str) -> tuple[str, str]:
    """返回 (base64_data_with_prefix, mime_type)"""
    if file_url_or_data.startswith("http://") or file_url_or_data.startswith("https://"):
        content, content_type = await fetch_url_content(file_url_or_data)
        b64_data = await asyncio.to_thread(_encode_bytes_to_base64_text, content)
        if not content_type:
            content_type = guess_mime_type(file_url_or_data)
        return build_data_uri(content_type, b64_data), content_type
    elif file_url_or_data.startswith("data:"):
        mime_type, _ = parse_data_uri(file_url_or_data)
        return file_url_or_data, mime_type
    else:
        return file_url_or_data, "application/octet-stream"
