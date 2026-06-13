"""Codex 身份混淆工具。

本模块把 CPA 的 Codex identity-confuse 逻辑翻译为 Python 实现。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any


_UTF8 = "utf-8"


def codex_identity_confuse_uuid(auth_id: str, kind: str, value: str) -> str:
    """生成确定性混淆 UUID（SHA1-based，对应 CPA 的 codexIdentityConfuseUUID）。"""
    # 修改原因：Codex 多账号轮换时，同一 OAuth 账号和同一原始身份标识必须稳定映射到同一个混淆 UUID。
    # 修改方式：沿用 CPA 的 name 格式，并使用 uuid5(NAMESPACE_OID, name)，uuid5 内部即 SHA-1 命名空间算法。
    # 目的：让请求侧混淆和响应侧还原在不同请求之间保持确定性，避免上游看到反代侧共享身份标识。
    name = f"cli-proxy-api:codex:identity-confuse:{kind}:{auth_id.strip()}:{value.strip()}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


def apply_codex_identity_confuse(headers: dict, payload: dict | bytes | str, auth_id: str) -> tuple[dict, dict | bytes | str, dict]:
    """请求侧身份混淆，返回混淆后的 headers、payload 和响应侧还原所需 state。"""
    # 修改原因：Codex 原生请求会携带 prompt、installation、turn、window 等客户端身份字段。
    # 修改方式：只在 auth_id 非空时对请求体和请求头中的已知身份字段做确定性 UUID 替换，并记录 bytes 替换表。
    # 目的：上游只看到按 OAuth 账号隔离后的混淆身份，客户端收到响应时仍可透明还原为原始身份。
    confused_headers = dict(headers or {})
    clean_auth_id = _clean_text(auth_id)
    state = _new_state(clean_auth_id, enabled=bool(clean_auth_id))
    if not clean_auth_id:
        return confused_headers, payload, state

    payload_obj, payload_kind = _payload_to_dict(payload)
    confused_payload = payload
    if payload_obj is not None:
        _apply_payload_identity_confuse(payload_obj, state)
        confused_payload = _dict_to_payload(payload_obj, payload_kind)

    _apply_header_identity_confuse(confused_headers, state)
    return confused_headers, confused_payload, state


def restore_codex_identity(data: bytes, state: dict) -> bytes:
    """响应侧还原：把混淆后的 ID 替换回原始值。"""
    # 修改原因：上游响应可能回显请求中的混淆身份，客户端需要继续看到自己发出的原始身份。
    # 修改方式：state 中的替换表在请求侧已编码为 bytes，这里按混淆值长度倒序执行 bytes.replace。
    # 目的：避免每次替换重复 encode，并避免 prompt UUID 先替换破坏 window UUID:0 这类包含关系。
    if not isinstance(data, (bytes, bytearray)):
        return data
    result = bytes(data)
    for original, confused in _iter_replacements(state, reverse=True):
        if confused and original and confused in result:
            result = result.replace(confused, original)
    return result


def confuse_codex_identity_response(data: bytes, state: dict) -> bytes:
    """响应侧混淆：把上游响应里可能出现的原始 ID 替换成混淆值。"""
    # 修改原因：部分上游或中间层可能返回未混淆的原始身份，继续转发会破坏身份隔离假设。
    # 修改方式：复用请求侧 state 的 original→confused bytes 映射，按原始值长度倒序执行正向替换。
    # 目的：在需要保持上游语义为混淆身份时，提供与还原函数对称的替换能力。
    if not isinstance(data, (bytes, bytearray)):
        return data
    result = bytes(data)
    for original, confused in _iter_replacements(state, reverse=False):
        if original and confused and original in result:
            result = result.replace(original, confused)
    return result


def _new_state(auth_id: str, enabled: bool) -> dict:
    # 修改原因：响应还原阶段只能可靠处理字节流，不能依赖 JSON 结构仍然存在。
    # 修改方式：state 同时保存少量文本状态和 original/confused 的 bytes 替换表。
    # 目的：请求侧只编码一次，响应侧可以直接做字节替换，行为与 CPA 的 bytes.ReplaceAll 对齐。
    return {
        "enabled": enabled,
        "auth_id": auth_id,
        "original_prompt_cache_key": "",
        "prompt_cache_key": "",
        "replacements": [],
        "_turn_ids": [],
    }


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode(_UTF8).strip()
        except UnicodeDecodeError:
            return ""
    return str(value).strip()


def _payload_to_dict(payload: dict | bytes | str) -> tuple[dict | None, str]:
    # 修改原因：普通路径传入 dict，透传或测试路径可能传入 JSON bytes/str。
    # 修改方式：dict 只复制顶层和 client_metadata，bytes/str 先按 UTF-8 JSON 解析。
    # 目的：兼容两类入口，同时避免把调用方持有的 headers/payload 原对象直接改坏。
    if isinstance(payload, dict):
        copied = dict(payload)
        metadata = copied.get("client_metadata")
        if isinstance(metadata, dict):
            copied["client_metadata"] = dict(metadata)
        return copied, "dict"
    if isinstance(payload, (bytes, bytearray)):
        try:
            decoded = bytes(payload).decode(_UTF8)
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            return None, "bytes"
        return (parsed, "bytes") if isinstance(parsed, dict) else (None, "bytes")
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None, "str"
        return (parsed, "str") if isinstance(parsed, dict) else (None, "str")
    return None, "unknown"


def _dict_to_payload(payload_obj: dict, payload_kind: str) -> dict | bytes | str:
    if payload_kind == "dict":
        return payload_obj
    encoded = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":"))
    if payload_kind == "bytes":
        return encoded.encode(_UTF8)
    return encoded


def _add_replacement(state: dict, kind: str, original: Any, confused: Any) -> None:
    original_text = _clean_text(original)
    confused_text = _clean_text(confused)
    if not state.get("enabled") or not original_text or not confused_text or original_text == confused_text:
        return

    pair = (original_text.encode(_UTF8), confused_text.encode(_UTF8))
    for existing_original, existing_confused in state.get("replacements", []):
        if existing_original == pair[0] and existing_confused == pair[1]:
            return
    state.setdefault("replacements", []).append(pair)

    if kind == "turn":
        for original_turn, confused_turn in state.get("_turn_ids", []):
            if original_turn == original_text and confused_turn == confused_text:
                return
        state.setdefault("_turn_ids", []).append((original_text, confused_text))


def _apply_payload_identity_confuse(payload_obj: dict, state: dict) -> None:
    prompt_cache_key = _clean_text(payload_obj.get("prompt_cache_key"))
    if prompt_cache_key:
        confused_prompt = codex_identity_confuse_uuid(state.get("auth_id", ""), "prompt-cache", prompt_cache_key)
        state["original_prompt_cache_key"] = prompt_cache_key
        state["prompt_cache_key"] = confused_prompt
        payload_obj["prompt_cache_key"] = confused_prompt
        _add_replacement(state, "prompt-cache", prompt_cache_key, confused_prompt)

    client_metadata = payload_obj.get("client_metadata")
    if not isinstance(client_metadata, dict):
        return

    installation_id = _clean_text(client_metadata.get("x-codex-installation-id"))
    if installation_id:
        confused_installation = codex_identity_confuse_uuid(state.get("auth_id", ""), "installation", installation_id)
        client_metadata["x-codex-installation-id"] = confused_installation
        _add_replacement(state, "installation", installation_id, confused_installation)

    if "x-codex-turn-metadata" in client_metadata:
        client_metadata["x-codex-turn-metadata"] = _apply_turn_metadata_identity_confuse(
            client_metadata.get("x-codex-turn-metadata"),
            state,
        )

    if state.get("prompt_cache_key"):
        window_id = _clean_text(client_metadata.get("x-codex-window-id"))
        if window_id:
            confused_window = f"{state['prompt_cache_key']}:0"
            client_metadata["x-codex-window-id"] = confused_window
            _add_replacement(state, "window", window_id, confused_window)


def _apply_header_identity_confuse(headers: dict, state: dict) -> None:
    raw_turn_metadata = _header_value_case_insensitive(headers, "X-Codex-Turn-Metadata")
    if _clean_text(raw_turn_metadata):
        _set_header_case_preserved(
            headers,
            "X-Codex-Turn-Metadata",
            _apply_turn_metadata_identity_confuse(raw_turn_metadata, state),
        )

    confused_prompt = state.get("prompt_cache_key")
    if not confused_prompt:
        return

    _set_header_case_preserved(headers, "Session-Id", confused_prompt)
    if _header_value_case_insensitive(headers, "session_id"):
        _set_header_case_preserved(headers, "session_id", confused_prompt)
    if _header_value_case_insensitive(headers, "Conversation_id"):
        _set_header_case_preserved(headers, "Conversation_id", confused_prompt)

    _set_header_case_preserved(headers, "X-Client-Request-Id", confused_prompt)
    _set_header_case_preserved(headers, "Thread-Id", confused_prompt)

    existing_window = _header_value_case_insensitive(headers, "X-Codex-Window-Id")
    confused_window = f"{confused_prompt}:0"
    if existing_window:
        _add_replacement(state, "window", existing_window, confused_window)
    _set_header_case_preserved(headers, "X-Codex-Window-Id", confused_window)


def _apply_turn_metadata_identity_confuse(raw_turn_metadata: Any, state: dict) -> Any:
    if not state.get("enabled"):
        return raw_turn_metadata
    if isinstance(raw_turn_metadata, dict):
        updated = dict(raw_turn_metadata)
        _apply_turn_metadata_dict_identity_confuse(updated, state)
        return updated

    raw_text = _clean_text(raw_turn_metadata)
    if not raw_text:
        return raw_turn_metadata

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return _apply_turn_metadata_text_identity_confuse(raw_text, state)

    if not isinstance(parsed, dict):
        return raw_turn_metadata
    _apply_turn_metadata_dict_identity_confuse(parsed, state)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _apply_turn_metadata_dict_identity_confuse(metadata: dict, state: dict) -> None:
    confused_prompt = state.get("prompt_cache_key")
    if confused_prompt and "prompt_cache_key" in metadata:
        metadata["prompt_cache_key"] = confused_prompt

    turn_id = _clean_text(metadata.get("turn_id"))
    if turn_id:
        metadata["turn_id"] = _confuse_turn_id(state, turn_id)

    if confused_prompt and "window_id" in metadata:
        window_id = _clean_text(metadata.get("window_id"))
        confused_window = f"{confused_prompt}:0"
        metadata["window_id"] = confused_window
        if window_id:
            _add_replacement(state, "window", window_id, confused_window)


def _apply_turn_metadata_text_identity_confuse(raw_text: str, state: dict) -> str:
    updated = raw_text
    confused_prompt = state.get("prompt_cache_key")
    original_prompt = state.get("original_prompt_cache_key")

    if confused_prompt:
        prompt_pattern = re.compile(r'("prompt_cache_key"\s*:\s*")([^"]*)(")')
        if prompt_pattern.search(updated):
            updated = prompt_pattern.sub(lambda match: f"{match.group(1)}{confused_prompt}{match.group(3)}", updated)
        elif original_prompt:
            updated = updated.replace(original_prompt, confused_prompt)

    turn_pattern = re.compile(r'("turn_id"\s*:\s*")([^"]+)(")')
    updated = turn_pattern.sub(
        lambda match: f"{match.group(1)}{_confuse_turn_id(state, match.group(2))}{match.group(3)}",
        updated,
    )

    if confused_prompt:
        window_pattern = re.compile(r'("window_id"\s*:\s*")([^"]*)(")')

        def replace_window(match: re.Match) -> str:
            confused_window = f"{confused_prompt}:0"
            _add_replacement(state, "window", match.group(2), confused_window)
            return f"{match.group(1)}{confused_window}{match.group(3)}"

        updated = window_pattern.sub(replace_window, updated)

    return updated


def _confuse_turn_id(state: dict, turn_id: Any) -> str:
    clean_turn_id = _clean_text(turn_id)
    if not state.get("enabled") or not state.get("auth_id") or not clean_turn_id:
        return clean_turn_id

    for original_turn, confused_turn in state.get("_turn_ids", []):
        if clean_turn_id == original_turn or clean_turn_id == confused_turn:
            return confused_turn

    confused_turn = codex_identity_confuse_uuid(state.get("auth_id", ""), "turn", clean_turn_id)
    _add_replacement(state, "turn", clean_turn_id, confused_turn)
    return confused_turn


def _header_key_case_insensitive(headers: dict, name: str) -> Any:
    target = name.lower()
    for key in headers.keys():
        if str(key).lower() == target:
            return key
    return None


def _header_value_case_insensitive(headers: dict, name: str) -> Any:
    key = _header_key_case_insensitive(headers, name)
    if key is None:
        return None
    return headers.get(key)


def _set_header_case_preserved(headers: dict, name: str, value: Any) -> None:
    key = _header_key_case_insensitive(headers, name)
    headers[key if key is not None else name] = value


def _iter_replacements(state: dict, reverse: bool) -> list[tuple[bytes, bytes]]:
    # 修改原因：prompt UUID 可能是 window UUID:0 的前缀，直接按插入顺序替换会产生部分替换。
    # 修改方式：还原时按 confused 长度倒序，混淆时按 original 长度倒序。
    # 目的：保证包含关系下仍能完整替换最长身份字段。
    replacements = []
    for pair in (state or {}).get("replacements", []):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        original, confused = pair
        if isinstance(original, bytes) and isinstance(confused, bytes):
            replacements.append((original, confused))
    index = 1 if reverse else 0
    return sorted(replacements, key=lambda item: len(item[index]), reverse=True)


# hashlib 保留为显式依赖说明：Codex 混淆 UUID 与 SHA-1 namespace UUID 语义绑定，实际计算由 uuid.uuid5 完成。
_hashlib_sha1 = hashlib.sha1
