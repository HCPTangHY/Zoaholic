"""文件转文本插件（file_to_text）

功能：
- 将请求消息中的文本类文件（text/*、代码、JSON、YAML 等）转换为内联文本
- 适用于不支持文件上传的渠道（如 DeepSeek）
- 图片和二进制文件不处理（交给 image_filter 或原样保留）

配置位置：
- provider.preferences.enabled_plugins 中添加 "file_to_text"

参数：
- 无参数或 "file_to_text" — 转换文件并用 ``` 代码块包裹，附带文件名
- "file_to_text:quiet" — 转换但不加文件名标题
- "file_to_text:remove" — 直接移除所有文件（不转换，类似 image_filter 的 quiet）

处理逻辑：
1. 遍历所有消息的 content（list 格式）
2. 识别 type 为 file / input_file 的项
3. 判断是否为文本类文件（通过 mime_type 或文件扩展名）
4. 解码 base64 内容，替换为 text 类型的内联文本
5. 非文本文件（图片、音频、二进制）不处理
"""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, List, Optional, Tuple

from core.log_config import logger
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
    get_plugin_options,
)


PLUGIN_INFO = {
    "name": "file_to_text",
    "version": "1.0.0",
    "description": "文件转文本插件 — 将文本类文件转换为内联文本，适用于不支持文件上传的渠道。支持 text/*、代码文件、JSON、YAML 等格式。",
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "category": "interceptors",
        "tags": ["filter", "file", "text", "convert"],
        "params_hint": "留空 = 转为代码块 + 文件名 | quiet = 转为纯文本 | remove = 直接移除文件",
        "params_schema": [
            {
                "key": "mode",
                "label": "转换模式",
                "type": "select",
                "options": [
                    {"value": "", "label": "代码块 + 文件名（默认）"},
                    {"value": "quiet", "label": "纯文本（无格式）"},
                    {"value": "remove", "label": "直接移除文件"},
                ],
                "default": "",
            },
        ],
    },
}

EXTENSIONS = [
    "interceptors:file_to_text_request",
]

# 被认为是文本的 MIME 类型前缀/精确匹配
_TEXT_MIME_PREFIXES = (
    "text/",
)

_TEXT_MIME_EXACT = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
    "application/typescript",
    "application/x-python",
    "application/x-python-code",
    "application/x-sh",
    "application/x-shellscript",
    "application/sql",
    "application/graphql",
    "application/toml",
    "application/x-toml",
    "application/ld+json",
    "application/xhtml+xml",
    "application/x-httpd-php",
    "application/x-ruby",
    "application/x-perl",
    "application/x-lua",
    "application/x-rust",
    "application/vnd.api+json",
    "application/schema+json",
    # CSV 虽然可能很大但是文本
    "application/csv",
}

# 通过扩展名推断是否为文本文件
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".ndjson",
    ".xml", ".html", ".htm", ".xhtml", ".svg",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".py", ".pyi", ".pyx",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
    ".cs", ".fs", ".fsx",
    ".go", ".rs", ".swift", ".dart", ".zig",
    ".rb", ".pl", ".pm", ".lua", ".php",
    ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd", ".ps1",
    ".sql", ".graphql", ".gql",
    ".r", ".R", ".jl", ".m", ".ex", ".exs", ".erl", ".hrl",
    ".css", ".scss", ".sass", ".less", ".styl",
    ".proto", ".thrift", ".avsc",
    ".dockerfile", ".makefile", ".cmake",
    ".tf", ".hcl",
    ".tex", ".bib", ".sty",
    ".gitignore", ".gitattributes", ".editorconfig",
    ".properties", ".lock",
}


def _is_text_mime(mime: str) -> bool:
    """判断 MIME 类型是否为文本"""
    if not mime:
        return False
    mime = mime.lower().split(";")[0].strip()  # 去掉 charset 等参数
    if any(mime.startswith(p) for p in _TEXT_MIME_PREFIXES):
        return True
    return mime in _TEXT_MIME_EXACT


def _is_text_filename(filename: str) -> bool:
    """通过文件名扩展名判断是否为文本"""
    if not filename:
        return False
    name_lower = filename.lower()
    # 无扩展名的常见文本文件
    basename = name_lower.rsplit("/", 1)[-1]
    if basename in {"dockerfile", "makefile", "cmakelists.txt", "rakefile",
                    "gemfile", "procfile", "vagrantfile", ".gitignore",
                    ".env", ".editorconfig"}:
        return True
    # 按扩展名判断
    for ext in _TEXT_EXTENSIONS:
        if name_lower.endswith(ext):
            return True
    return False


def _guess_lang(filename: str, mime: str) -> str:
    """猜测代码语言用于 markdown 代码块标记"""
    if filename:
        ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
        lang_map = {
            "py": "python", "pyi": "python", "js": "javascript", "mjs": "javascript",
            "ts": "typescript", "tsx": "tsx", "jsx": "jsx",
            "rb": "ruby", "rs": "rust", "go": "go", "java": "java",
            "kt": "kotlin", "cs": "csharp", "cpp": "cpp", "c": "c",
            "h": "c", "hpp": "cpp", "swift": "swift", "dart": "dart",
            "sh": "bash", "bash": "bash", "zsh": "zsh",
            "sql": "sql", "json": "json", "yaml": "yaml", "yml": "yaml",
            "xml": "xml", "html": "html", "css": "css", "scss": "scss",
            "md": "markdown", "toml": "toml", "ini": "ini",
            "php": "php", "lua": "lua", "r": "r", "jl": "julia",
            "pl": "perl", "ex": "elixir", "erl": "erlang",
            "proto": "protobuf", "graphql": "graphql", "tf": "terraform",
            "tex": "latex", "vue": "vue", "svelte": "svelte",
        }
        if ext in lang_map:
            return lang_map[ext]
    if mime:
        if "json" in mime:
            return "json"
        if "xml" in mime or "html" in mime:
            return "xml"
        if "yaml" in mime:
            return "yaml"
        if "javascript" in mime:
            return "javascript"
    return ""


def _decode_content(data_str: str, mime: str = "") -> Optional[str]:
    """从 base64 / data URI 解码文本内容"""
    try:
        # data URI: data:mime;base64,xxxxx
        if data_str.startswith("data:"):
            match = re.match(r"data:[^;]*;base64,(.+)", data_str, re.DOTALL)
            if match:
                raw = base64.b64decode(match.group(1))
            else:
                # 可能是 data:text/plain,xxxxx（非 base64）
                _, _, content = data_str.partition(",")
                return content
        else:
            # 纯 base64
            raw = base64.b64decode(data_str)

        # 尝试 UTF-8 解码
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            # 尝试 latin-1 兜底（几乎不会失败）
            try:
                return raw.decode("latin-1")
            except Exception:
                return None
    except Exception:
        return None


def _is_text_file_item(item: Dict[str, Any]) -> bool:
    """判断一个 content item 是否为文本类文件"""
    item_type = item.get("type", "")

    if item_type == "file":
        file_info = item.get("file", {})
        if not isinstance(file_info, dict):
            return False
        mime = file_info.get("mime_type", "")
        filename = file_info.get("filename", "")
        # 图片、音频、视频排除
        if mime and (mime.startswith("image/") or mime.startswith("audio/") or mime.startswith("video/")):
            return False
        if _is_text_mime(mime):
            return True
        if _is_text_filename(filename):
            return True
        # URL 是 data URI 且 mime 是文本
        url = file_info.get("url", "")
        if url.startswith("data:"):
            uri_mime = url.split(";")[0].replace("data:", "")
            if _is_text_mime(uri_mime):
                return True
        return False

    if item_type == "input_file":
        filename = item.get("filename", "")
        file_data = item.get("file_data", "")
        # 从 file_data 的 data URI 提取 mime
        if file_data and file_data.startswith("data:"):
            uri_mime = file_data.split(";")[0].replace("data:", "")
            if uri_mime.startswith("image/") or uri_mime.startswith("audio/") or uri_mime.startswith("video/"):
                return False
            if _is_text_mime(uri_mime):
                return True
        if _is_text_filename(filename):
            return True
        return False

    return False


def _extract_file_text(item: Dict[str, Any]) -> Tuple[Optional[str], str, str]:
    """提取文件的文本内容、文件名和 MIME 类型。
    返回 (decoded_text, filename, mime)
    """
    item_type = item.get("type", "")

    if item_type == "file":
        file_info = item.get("file", {})
        mime = file_info.get("mime_type", "")
        filename = file_info.get("filename", "")
        # 优先用 data 字段
        data = file_info.get("data", "")
        if data:
            text = _decode_content(data, mime)
            if text is not None:
                return text, filename, mime
        # 再试 url 字段（可能是 data URI）
        url = file_info.get("url", "")
        if url and url.startswith("data:"):
            text = _decode_content(url, mime)
            if text is not None:
                return text, filename, mime
        # url 是 http(s) 链接 — 无法在拦截器里下载，跳过
        return None, filename, mime

    if item_type == "input_file":
        filename = item.get("filename", "")
        file_data = item.get("file_data", "")
        file_url = item.get("file_url", "")
        # 从 data URI 提取 mime
        mime = ""
        source = file_data or file_url
        if source and source.startswith("data:"):
            mime = source.split(";")[0].replace("data:", "")
        if file_data:
            text = _decode_content(file_data, mime)
            if text is not None:
                return text, filename, mime
        if file_url and file_url.startswith("data:"):
            text = _decode_content(file_url, mime)
            if text is not None:
                return text, filename, mime
        return None, filename, mime

    return None, "", ""


def _convert_messages(payload: Dict[str, Any], mode: str) -> int:
    """转换 payload 中消息的文本文件为内联文本。返回转换数量。"""
    messages = payload.get("messages") or payload.get("input") or []
    converted_count = 0

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue

        new_content = []
        changed = False

        for item in content:
            if not isinstance(item, dict):
                new_content.append(item)
                continue

            if not _is_text_file_item(item):
                new_content.append(item)
                continue

            # 是文本文件
            if mode == "remove":
                changed = True
                converted_count += 1
                continue  # 直接丢弃

            text, filename, mime = _extract_file_text(item)
            if text is None:
                # 无法解码（可能是 URL 引用），保留原样
                new_content.append(item)
                continue

            changed = True
            converted_count += 1

            if mode == "quiet":
                # 纯文本，不加装饰
                new_content.append({"type": "text", "text": text})
            else:
                # 默认：代码块 + 文件名
                lang = _guess_lang(filename, mime)
                header = f"📄 {filename}\n" if filename else ""
                block = f"{header}```{lang}\n{text}\n```"
                new_content.append({"type": "text", "text": block})

        if changed:
            if new_content:
                msg["content"] = new_content
            else:
                msg["content"] = "[文件已移除]"

    return converted_count


async def _file_to_text_interceptor(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """请求拦截器：将文本类文件转换为内联文本"""
    options = get_plugin_options(provider, "file_to_text") or ""
    mode = options.lower().strip()
    if mode not in ("quiet", "remove"):
        mode = "default"

    count = _convert_messages(payload, mode)
    if count > 0:
        logger.info(f"[file_to_text] 已转换 {count} 个文件为内联文本 (provider={provider.get('provider', '?')}, mode={mode})")

    return url, headers, payload


def setup(manager):
    """注册拦截器"""
    register_request_interceptor(
        "file_to_text_request",
        _file_to_text_interceptor,
        priority=8,  # 比 image_filter (10) 更早执行
        plugin_name="file_to_text",
    )
    logger.info("[file_to_text] 插件已加载")


def teardown(manager):
    """卸载拦截器"""
    unregister_request_interceptor("file_to_text_request")
    logger.info("[file_to_text] 插件已卸载")
