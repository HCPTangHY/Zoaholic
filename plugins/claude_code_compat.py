"""
Claude Code 完整伪装插件。

功能：
1. 将普通 Claude 渠道的 x-api-key 认证改写为 Claude Code OAuth 风格的 Authorization: Bearer。
2. 注入 Claude Code 请求头，包括完整 anthropic-beta、User-Agent、Session-Id、request-id、Stainless SDK 头和 X-App。
3. 对 /messages payload 执行 Layer 1 到 Layer 6 的 plan billing 清洗。

Layer 7 响应反向映射通过响应拦截器实现，自动将上游 Claude 返回的
PascalCase tool name（如 Bash）和重命名后的 property name 还原为客户端原始名。

使用方式：
preferences:
  enabled_plugins:
    - claude_code_compat

参数格式保持向后兼容：
- claude_code_compat
- claude_code_compat:2.1.97
- claude_code_compat:2.1.97,cli
- claude_code_compat:2.1.97,cli,59cf53e54c78
- claude_code_compat:2.1.97,,59cf53e54c78
"""

import hashlib
import os
import re
import time
import uuid

from contextvars import ContextVar

from core.log_config import logger
from core.plugins import (
    get_plugin_options,
    register_request_interceptor,
    unregister_request_interceptor,
    register_response_interceptor,
    unregister_response_interceptor,
)


PLUGIN_INFO = {
    "name": "claude_code_compat",
    "version": "2.0.0",
    "description": "Claude Code 完整伪装插件（7 层 plan billing 清洗 + 头部注入）",
    "author": "Zoaholic",
    "dependencies": [],
    "metadata": {
        "category": "interceptor",
        "tags": ["claude", "anthropic", "claude-code", "auth", "bearer", "billing", "sanitize"],
        "params_hint": "格式：2.1.97 或 2.1.97,cli 或 2.1.97,cli,59cf53e54c78。留空使用默认值。",
        "params_schema": [
            {
                "key": "version",
                "label": "CC 版本号",
                "type": "text",
                "placeholder": "如 2.1.128",
                "default": "",
            },
            {
                "key": "client",
                "label": "客户端类型",
                "type": "select",
                "options": [
                    {"value": "", "label": "默认"},
                    {"value": "cli", "label": "CLI"},
                    {"value": "vscode", "label": "VSCode"},
                ],
                "default": "",
            },
        ],
    },
}

EXTENSIONS = [
    "interceptors:claude_code_compat_request",
    "interceptors:claude_code_compat_response",
]

# Layer 7: 请求级反向映射表（ContextVar 在请求生命周期内传递给响应拦截器）
_reverse_tool_map: ContextVar[dict] = ContextVar("_ccc_reverse_tool_map", default={})
_reverse_prop_map: ContextVar[dict] = ContextVar("_ccc_reverse_prop_map", default={})

# 修改原因：普通 Claude 渠道挂载插件时，需要与 claude-code 核心 channel 使用同一组默认版本和 billing 参数。
# 修改方式：将核心 channel 的默认版本、billing salt、采样索引和 header 前缀提升为插件常量。
# 目的：让插件生成的 User-Agent 和 billing header 与 Claude Code 伪装逻辑保持一致。
DEFAULT_CLAUDE_CODE_VERSION = "2.1.97"
DEFAULT_BILLING_SALT = "59cf53e54c78"
DEFAULT_ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
DEFAULT_ENTRYPOINT = "cli"
BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"
BILLING_SAMPLE_INDEXES = (4, 7, 20)
CLAUDE_CODE_IDENTITY_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."

# 修改原因：Claude Code OAuth 请求固定带有比普通 Claude adapter 更多的 beta flags。
# 修改方式：完整复制核心 channel 的 CLAUDE_CODE_ANTHROPIC_BETA 常量。
# 目的：防止普通 Claude 渠道仅带 tools beta，缺少 OAuth、thinking、redact 和 token-efficient tools 等标识。
CLAUDE_CODE_ANTHROPIC_BETA = (
    "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,"
    "structured-outputs-2025-12-15,fast-mode-2026-02-01,"
    "redact-thinking-2026-02-12,token-efficient-tools-2026-03-28"
)

# 修改原因：X-Claude-Code-Session-Id 需要在同一个 api key 上稳定一段时间，而不是每次请求变化。
# 修改方式：用模块级缓存保存 api key 到 UUID 的映射，并用 monotonic 时间控制 1 小时 TTL。
# 目的：模拟 Claude Code 客户端会话头的稳定性，同时避免长期复用旧 session id。
SESSION_ID_CACHE = {}
SESSION_TTL_SECONDS = 3600

# 修改原因：第三方客户端和代理会在 system/messages 中留下可识别字符串。
# 修改方式：使用大小写不敏感正则复制核心 channel 的第三方特征串清洗列表。
# 目的：降低请求被识别为第三方代理或 IDE 客户端的概率。
THIRD_PARTY_PATTERNS = re.compile(
    r"(?i)"
    r"(?:sessions_spawn|sessions_list|sessions_history|sessions_send|sessions_yield"
    r"|sessions_store|sessions_yield_interrupt"
    r"|HEARTBEAT_OK|HEARTBEAT"
    r"|clawhub|clawd|openclaw|open.?claw|cline|continue\.dev|lossless-claw"
    r"|running\s+inside|prometheus|skillhub"
    r"|roo.?code|windsurf|cursor|aider"
    r"|billing.?proxy|routing.?layer)"
)

# 修改原因：第三方工具名常使用小写或代理自定义名称，容易暴露客户端来源。
# 修改方式：复制核心 channel 的完整工具名 PascalCase 映射表。
# 目的：让 tools 数组和 messages 历史里的 tool_use/tool_result 名称更接近 Claude Code。
TOOL_RENAME_MAP = {
    "bash": "Bash",
    "read": "Read",
    "write": "Write",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "task": "Task",
    "webfetch": "WebFetch",
    "web_fetch": "WebFetch",

    "todowrite": "TodoWrite",
    "todoread": "TodoRead",
    "question": "Question",
    "skill": "Skill",
    "ls": "LS",
    "notebookedit": "NotebookEdit",
    "exec": "Bash",
    "process": "BashSession",
    "browser": "BrowserControl",
    "message": "SendMessage",
    "agents_list": "AgentList",
    "list_tasks": "TaskList",
    "get_history": "TaskHistory",
    "send_to_task": "TaskSend",
    "create_task": "TaskCreate",
    "subagents": "AgentControl",
    "session_status": "StatusCheck",
    "pdf": "PdfParse",
    "image_generate": "ImageCreate",
    "memory_search": "KnowledgeSearch",
    "memory_get": "KnowledgeGet",
    "lcm_expand_query": "ContextQuery",
    "lcm_grep": "ContextGrep",
    "lcm_describe": "ContextDescribe",
    "lcm_expand": "ContextExpand",
    "yield_task": "TaskYield",
    "task_store": "TaskStore",
    "task_yield_interrupt": "TaskYieldInterrupt",
}

# 修改原因：部分代理会把内部配置节直接写入 system prompt。
# 修改方式：复制核心 channel 的 section header 正则，仅去掉配置节标题行。
# 目的：减少 Tooling、Workspace、Messaging 等结构化配置指纹。
SYSTEM_CONFIG_SECTIONS = re.compile(
    r"(?:^|\\n|\n)"
    r"##\s*(?:Tooling|Workspace|Messaging|Reply|Configuration|Sessions?|Scheduling|Browser)"
    r"(?:\s|\\n|\n|$)",
    re.MULTILINE,
)

# 修改原因：第三方工具 schema 里的属性名会暴露会话、摘要、唤醒等代理实现细节。
# 修改方式：复制核心 channel 的属性名重命名映射，并同步更新 required 列表。
# 目的：让工具 schema 字段更接近普通任务参数，减少第三方特征。
PROP_RENAME_MAP = {
    "session_id": "thread_id",
    "conversation_id": "thread_ref",
    "summaryIds": "chunk_ids",
    "summary_id": "chunk_id",
    "system_event": "event_text",
    "agent_id": "worker_id",
    "wake_at": "trigger_at",
    "wake_event": "trigger_event",
}

# 修改原因：真实 Claude Code session 通常包含一组标准工具。
# 修改方式：复制核心 channel 注入的工具桩，并在已有工具前补齐缺失项。
# 目的：让 tools 数组轮廓更像 Claude Code，而不改变实际可调用的原有工具定义。
CC_TOOL_STUBS = [
    {
        "name": "Glob",
        "description": "Find files by pattern",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "File glob pattern"}},
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "Grep",
        "description": "Search file contents",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search pattern"},
                "path": {"type": "string", "description": "Directory path"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },

    {
        "name": "NotebookEdit",
        "description": "Edit notebook cells",
        "input_schema": {
            "type": "object",
            "properties": {
                "notebook_path": {"type": "string", "description": "Path to notebook"},
                "cell_index": {"type": "integer", "description": "Cell index"},
            },
            "required": ["notebook_path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "TodoRead",
        "description": "Read task list",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

SKIPPED_CONTENT_BLOCK_TYPES = {"thinking", "redacted_thinking"}


def get_header_key_value_case_insensitive(headers, name):
    """按大小写不敏感方式读取请求头，返回原始 key 和 value。"""
    if not isinstance(headers, dict):
        return None, None
    target = str(name).lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return key, value
    return None, None


def get_header_case_insensitive(headers, name):
    """按大小写不敏感方式读取请求头值。"""
    return get_header_key_value_case_insensitive(headers, name)[1]


def pop_header_case_insensitive(headers, name):
    """按大小写不敏感方式移除请求头并返回其值。"""
    if not isinstance(headers, dict):
        return None
    target = str(name).lower()
    for key in list(headers.keys()):
        if str(key).lower() == target:
            return headers.pop(key)
    return None


def set_header_case_insensitive(headers, name, value):
    """按大小写不敏感方式设置请求头，避免同时存在不同大小写的重复键。"""
    pop_header_case_insensitive(headers, name)
    headers[name] = value


def merge_anthropic_beta(headers):
    """把已有 anthropic-beta 与 Claude Code 完整 beta flags 合并去重。"""
    existing_key, existing_value = get_header_key_value_case_insensitive(headers, "anthropic-beta")
    beta_values = []
    for raw in (existing_value or "", CLAUDE_CODE_ANTHROPIC_BETA):
        for item in str(raw).split(","):
            beta = item.strip()
            if beta and beta not in beta_values:
                beta_values.append(beta)
    if existing_key and existing_key != "anthropic-beta":
        headers.pop(existing_key, None)
    headers["anthropic-beta"] = ",".join(beta_values)


def get_session_id(api_key):
    """获取 per-apiKey 的稳定 session UUID，TTL 为 1 小时。"""
    cache_key = str(api_key or "")
    now = time.monotonic()
    cached = SESSION_ID_CACHE.get(cache_key)
    if cached and (now - cached[1]) < SESSION_TTL_SECONDS:
        return cached[0]
    session_id = str(uuid.uuid4())
    SESSION_ID_CACHE[cache_key] = (session_id, now)
    return session_id


def extract_bearer_token(value):
    """从 Authorization 头中提取 Bearer token，供 Session-Id 缓存使用。"""
    text = str(value or "").strip()
    if text.lower().startswith("bearer "):
        return text.split(None, 1)[1]
    return text


def resolve_plugin_config(provider):
    """解析 claude_code_compat 的简单参数配置。"""
    version = DEFAULT_CLAUDE_CODE_VERSION
    entrypoint = os.getenv(DEFAULT_ENTRYPOINT_ENV) or DEFAULT_ENTRYPOINT
    billing_salt = DEFAULT_BILLING_SALT

    plugin_options = get_plugin_options(PLUGIN_INFO["name"], provider)
    if plugin_options:
        parts = [part.strip() for part in str(plugin_options).split(",")]
        if parts and parts[0]:
            version = parts[0]
        if len(parts) > 1 and parts[1]:
            entrypoint = parts[1]
        if len(parts) > 2 and parts[2]:
            billing_salt = parts[2]

    return {
        "version": version,
        "entrypoint": entrypoint,
        "user_agent": f"claude-code/{version}",
        "billing_salt": billing_salt,
    }


def parse_version_from_ua(ua, default_version=DEFAULT_CLAUDE_CODE_VERSION):
    """从 User-Agent 解析 Claude Code 版本号，如 claude-code/2.1.97。"""
    if not ua:
        return default_version
    for part in str(ua).split():
        if part.startswith("claude-code/"):
            version = part.split("/", 1)[1].strip()
            if version:
                return version
    return default_version


def parse_entrypoint_from_ua(ua, default_entrypoint=DEFAULT_ENTRYPOINT):
    """从 User-Agent 解析 entrypoint，如 claude-code/2.1.97 vscode。"""
    if not ua:
        return default_entrypoint
    parts = str(ua).split()
    for index, part in enumerate(parts):
        if part.startswith("claude-code/") and index + 1 < len(parts):
            entrypoint = parts[index + 1].lower()
            if entrypoint in ("cli", "vscode", "local-agent", "jetbrains", "emacs", "vim"):
                return entrypoint
    return default_entrypoint


def sample_js_code_unit(text, index):
    """按 JavaScript UTF-16 code unit 语义采样单个字符。"""
    if not isinstance(text, str) or index < 0:
        return "0"
    utf16_le = text.encode("utf-16-le")
    start = index * 2
    end = start + 2
    if end > len(utf16_le):
        return "0"
    return utf16_le[start:end].decode("utf-16-le", errors="replace")


def first_user_message_text(messages):
    """提取第一条 user 消息中的首个文本内容。"""
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
                if isinstance(item, str):
                    return item
        return ""
    return ""


def build_billing_header(messages, version=DEFAULT_CLAUDE_CODE_VERSION, entrypoint=DEFAULT_ENTRYPOINT, billing_salt=DEFAULT_BILLING_SALT):
    """构造 x-anthropic-billing-header 文本。"""
    sampled = "".join(sample_js_code_unit(first_user_message_text(messages), index) for index in BILLING_SAMPLE_INDEXES)
    digest = hashlib.sha256(f"{billing_salt}{sampled}{version}".encode("utf-8")).hexdigest()[:3]
    return (
        f"{BILLING_HEADER_PREFIX} cc_version={version}.{digest}; "
        f"cc_entrypoint={entrypoint}; cch=00000;"
    )


def has_billing_header(system):
    """检查 system 中是否已经存在 billing header。"""
    if isinstance(system, str):
        return system.strip().startswith(BILLING_HEADER_PREFIX)
    if isinstance(system, list) and system:
        first_item = system[0]
        if isinstance(first_item, dict):
            text = first_item.get("text")
            return isinstance(text, str) and text.strip().startswith(BILLING_HEADER_PREFIX)
        if isinstance(first_item, str):
            return first_item.strip().startswith(BILLING_HEADER_PREFIX)
    return False


def system_is_empty(system):
    """判断 system 是否完全为空，供 Claude Code 身份声明注入使用。"""
    return system is None or (isinstance(system, str) and not system.strip()) or (isinstance(system, list) and not system)


def ensure_billing_header(payload, headers, billing_salt=DEFAULT_BILLING_SALT, default_version=DEFAULT_CLAUDE_CODE_VERSION, default_entrypoint=DEFAULT_ENTRYPOINT):
    """Layer 1：注入 billing header，并在 system 为空时补 Claude Code 身份声明。"""
    system = payload.get("system")
    if has_billing_header(system):
        return

    ua = str(get_header_case_insensitive(headers, "User-Agent") or "")
    version = parse_version_from_ua(ua, default_version)
    entrypoint = parse_entrypoint_from_ua(ua, default_entrypoint)
    billing_block = {
        "type": "text",
        "text": build_billing_header(payload.get("messages", []), version, entrypoint, billing_salt),
    }

    if system_is_empty(system):
        payload["system"] = [
            billing_block,
            {"type": "text", "text": CLAUDE_CODE_IDENTITY_TEXT},
        ]
    elif isinstance(system, str):
        payload["system"] = [billing_block, {"type": "text", "text": system}]
    elif isinstance(system, list):
        payload["system"] = [billing_block, *system]
    else:
        payload["system"] = [billing_block, system]


def clean_third_party_text(text):
    """Layer 2：清理单段文本中的第三方特征串。"""
    if not isinstance(text, str):
        return text
    return THIRD_PARTY_PATTERNS.sub("", text)


def strip_system_config_text(text):
    """Layer 4：清理单段 system 文本中的配置节 header。"""
    if not isinstance(text, str):
        return text
    return SYSTEM_CONFIG_SECTIONS.sub("\n", text)


def sanitize_third_party_strings(payload):
    """Layer 2：清洗 system 和 messages 中的第三方特征串，跳过 thinking/redacted_thinking。"""
    system = payload.get("system")
    if isinstance(system, str):
        payload["system"] = clean_third_party_text(system)
    elif isinstance(system, list):
        for index, block in enumerate(system):
            if isinstance(block, str):
                system[index] = clean_third_party_text(block)
            elif isinstance(block, dict) and block.get("type") not in SKIPPED_CONTENT_BLOCK_TYPES:
                if isinstance(block.get("text"), str):
                    block["text"] = clean_third_party_text(block["text"])

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = clean_third_party_text(content)
        elif isinstance(content, list):
            for index, block in enumerate(content):
                if isinstance(block, str):
                    content[index] = clean_third_party_text(block)
                elif isinstance(block, dict) and block.get("type") not in SKIPPED_CONTENT_BLOCK_TYPES:
                    if isinstance(block.get("text"), str):
                        block["text"] = clean_third_party_text(block["text"])
                    if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                        block["content"] = clean_third_party_text(block["content"])


def strip_system_config_sections(payload):
    """Layer 4：从 system prompt 中去掉 Tooling、Workspace、Messaging 等配置节标题。"""
    system = payload.get("system")
    if isinstance(system, str):
        payload["system"] = strip_system_config_text(system)
    elif isinstance(system, list):
        for index, block in enumerate(system):
            if isinstance(block, str):
                system[index] = strip_system_config_text(block)
            elif isinstance(block, dict) and block.get("type") not in SKIPPED_CONTENT_BLOCK_TYPES:
                if isinstance(block.get("text"), str):
                    block["text"] = strip_system_config_text(block["text"])


def rename_tools_in_messages(messages, renamed):
    """Layer 3：同步重命名 messages 历史中的 tool_use/tool_result name。"""
    if not isinstance(messages, list) or not renamed:
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_use", "tool_result"):
                name = block.get("name")
                if name in renamed:
                    block["name"] = renamed[name]


def rename_tools(payload):
    """Layer 3：把 tools 数组里的第三方工具名重命名为 Claude Code PascalCase。"""
    renamed = {}
    tools = payload.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name")
            if not isinstance(name, str):
                continue
            new_name = TOOL_RENAME_MAP.get(name.lower())
            if new_name and name != new_name:
                renamed[name] = new_name
                tool["name"] = new_name

    rename_tools_in_messages(payload.get("messages"), renamed)
    return renamed


def clear_description_fields(value):
    """Layer 5：递归清空 schema 中的 description 字段。"""
    if isinstance(value, dict):
        for key, nested in list(value.items()):
            if key == "description":
                value[key] = ""
            else:
                clear_description_fields(nested)
    elif isinstance(value, list):
        for item in value:
            clear_description_fields(item)


def strip_tool_descriptions(tools):
    """Layer 5：清空现有工具和嵌套属性的 description，减少 schema 指纹。"""
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if "description" in tool:
            tool["description"] = ""
        schema = tool.get("input_schema")
        if isinstance(schema, dict):
            clear_description_fields(schema)


def clone_json_like(value):
    """复制由 dict/list/基础类型组成的工具桩，避免多次请求共享同一嵌套对象。"""
    if isinstance(value, dict):
        return {key: clone_json_like(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [clone_json_like(item) for item in value]
    return value


def inject_cc_tool_stubs(tools):
    """Layer 5：向 tools 数组补齐 Claude Code 标准工具桩。"""
    if not isinstance(tools, list):
        return
    existing_names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
    missing_stubs = []
    for stub in CC_TOOL_STUBS:
        if stub["name"] not in existing_names:
            missing_stubs.append(clone_json_like(stub))
            existing_names.add(stub["name"])
    tools[0:0] = missing_stubs


def rename_tool_properties(tools):
    """Layer 6：重命名工具 input_schema.properties 中的第三方属性名，并同步 required。"""
    if not isinstance(tools, list):
        return
    for tool in tools:
        schema = tool.get("input_schema") if isinstance(tool, dict) else None
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for old_name, new_name in PROP_RENAME_MAP.items():
                if old_name in properties:
                    properties[new_name] = properties.pop(old_name)
        required = schema.get("required")
        if isinstance(required, list):
            schema["required"] = [PROP_RENAME_MAP.get(item, item) for item in required]


def sanitize_payload(payload, headers, billing_salt=DEFAULT_BILLING_SALT, default_version=DEFAULT_CLAUDE_CODE_VERSION, default_entrypoint=DEFAULT_ENTRYPOINT):
    """按 Layer 1 到 Layer 6 顺序清洗 Claude /messages payload。"""
    if not isinstance(payload, dict):
        return payload

    # Layer 1：注入 billing header 和缺省 Claude Code 身份声明。
    ensure_billing_header(payload, headers, billing_salt, default_version, default_entrypoint)

    # Layer 2：清理第三方特征串，明确跳过 thinking/redacted_thinking block。
    sanitize_third_party_strings(payload)

    # Layer 3：重命名 tools 和历史消息里的 tool_use/tool_result 名称。
    renamed = rename_tools(payload)

    # 保存 Layer 3 反向映射到 ContextVar，供 Layer 7 响应拦截器使用
    if renamed:
        _reverse_tool_map.set({v: k for k, v in renamed.items()})

    # Layer 4：去除 system prompt 配置节标题。
    strip_system_config_sections(payload)

    # Layer 5：清空现有工具描述，然后补齐 Claude Code 工具桩。
    tools = payload.get("tools")
    strip_tool_descriptions(tools)
    inject_cc_tool_stubs(tools)

    # Layer 6：重命名工具 schema property，并同步 required。
    rename_tool_properties(tools)

    # 保存 Layer 6 反向映射到 ContextVar
    _reverse_prop_map.set({v: k for k, v in PROP_RENAME_MAP.items()})

    return payload


def apply_claude_code_headers(headers, api_key=None, config=None):
    """把普通 Claude 请求头改成完整 Claude Code OAuth 请求头。"""
    if not isinstance(headers, dict):
        return

    resolved_config = config or {}
    version = resolved_config.get("version") or DEFAULT_CLAUDE_CODE_VERSION
    user_agent = resolved_config.get("user_agent") or f"claude-code/{version}"

    # 修改原因：普通 Claude adapter 默认发送 x-api-key，但 Claude Code OAuth 使用 Bearer token。
    # 修改方式：移除 x-api-key，并优先用传入 api_key 生成 Authorization；没有 api_key 时使用原 x-api-key 值。
    # 目的：让普通 Claude 渠道也能按 Claude Code OAuth 的认证形态请求上游。
    header_api_key = pop_header_case_insensitive(headers, "x-api-key")
    credential = api_key if api_key not in (None, "") else header_api_key
    if credential not in (None, ""):
        set_header_case_insensitive(headers, "Authorization", f"Bearer {credential}")

    merge_anthropic_beta(headers)
    set_header_case_insensitive(headers, "User-Agent", user_agent)
    set_header_case_insensitive(headers, "X-App", "cli")

    authorization_token = extract_bearer_token(get_header_case_insensitive(headers, "Authorization"))
    session_key = credential if credential not in (None, "") else authorization_token
    if session_key and get_header_case_insensitive(headers, "X-Claude-Code-Session-Id") is None:
        headers["X-Claude-Code-Session-Id"] = get_session_id(session_key)

    if get_header_case_insensitive(headers, "x-client-request-id") is None:
        headers["x-client-request-id"] = str(uuid.uuid4())

    stainless_defaults = [
        ("X-Stainless-Retry-Count", "0"),
        ("X-Stainless-Runtime", "node"),
        ("X-Stainless-Lang", "js"),
        ("X-Stainless-Timeout", "600"),
    ]
    for header_name, value in stainless_defaults:
        if get_header_case_insensitive(headers, header_name) is None:
            headers[header_name] = value


def is_claude_request(engine, url, headers):
    """判断是否为 Claude/Anthropic 请求。"""
    engine_lower = str(engine or "").lower()
    if engine_lower in {"claude", "anthropic"}:
        return True
    if get_header_case_insensitive(headers, "anthropic-version") is not None:
        return True
    if get_header_case_insensitive(headers, "x-api-key") is None:
        return False
    url_lower = str(url or "").lower()
    return "/messages" in url_lower or "/models" in url_lower


def is_message_request(url, payload):
    """判断当前是否为 Claude messages 请求。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return False
    url_lower = str(url or "").lower()
    return url_lower.endswith("/messages") or "/messages?" in url_lower or "/messages/" in url_lower


def _is_native_claude_code(request, headers):
    """检测请求是否来自原生 Claude Code 客户端。优先读客户端原始 UA。"""
    # 先从客户端原始请求头读
    client_ua = ""
    if request is not None and hasattr(request, "headers"):
        client_ua = str(request.headers.get("user-agent") or "")
    # fallback 到上游构建的 headers
    if not client_ua:
        client_ua = str(get_header_case_insensitive(headers, "User-Agent") or "")
    return "claude-code/" in client_ua or "claude-cli/" in client_ua


async def claude_code_compat_request_interceptor(request, engine, provider, api_key, url, headers, payload):
    """Claude Code 完整伪装请求拦截器。原生 CC 请求直接放行。"""
    if not is_claude_request(engine, url, headers):
        return url, headers, payload

    _native = _is_native_claude_code(request, headers)
    if _native:
        logger.debug("[claude_code_compat] native CC detected, key transform + header completion only")
        # 原生 CC 跳过伪装/桩注入/payload清洗，但补全关键 header：
        # 1. x-api-key → Bearer 转换
        header_api_key = pop_header_case_insensitive(headers, "x-api-key")
        credential = api_key if api_key not in (None, "") else header_api_key
        if credential:
            set_header_case_insensitive(headers, "Authorization", f"Bearer {credential}")
        # 2. anthropic-beta 合并补全（保留用户原始 flags + 补充缺失的）
        merge_anthropic_beta(headers)
        # 3. anthropic-version 补全（用户没带就补）
        if get_header_case_insensitive(headers, "anthropic-version") is None:
            headers["anthropic-version"] = "2023-06-01"
        # 4. User-Agent — 原生 CC 自带，不覆盖
        return url, headers, payload

    config = resolve_plugin_config(provider)
    apply_claude_code_headers(headers, api_key, config)

    if is_message_request(url, payload):
        sanitize_payload(
            payload,
            headers,
            billing_salt=config["billing_salt"],
            default_version=config["version"],
            default_entrypoint=config["entrypoint"],
        )

    logger.debug(
        "[claude_code_compat] applied. url=%s, version=%s, user_agent=%s",
        url,
        config["version"],
        config["user_agent"],
    )
    return url, headers, payload


async def claude_code_compat_response_interceptor(response_chunk, engine, model, is_stream):
    """Layer 7: 响应反向映射 — 把 CC PascalCase tool name 和 property name 改回客户端原始名。"""
    if not isinstance(response_chunk, str):
        return response_chunk

    reverse_tools = _reverse_tool_map.get({})
    reverse_props = _reverse_prop_map.get({})
    if not reverse_tools and not reverse_props:
        return response_chunk

    chunk = response_chunk

    # 反向 tool name（精准匹配 JSON 值位置）
    for cc_name, orig_name in reverse_tools.items():
        chunk = chunk.replace(f'"name":"{cc_name}"', f'"name":"{orig_name}"')
        chunk = chunk.replace(f'"name": "{cc_name}"', f'"name": "{orig_name}"')

    # 反向 property name
    for renamed, orig in reverse_props.items():
        chunk = chunk.replace(f'"{renamed}"', f'"{orig}"')

    return chunk


def setup(manager):
    """插件初始化。"""
    register_request_interceptor(
        interceptor_id="claude_code_compat_request",
        callback=claude_code_compat_request_interceptor,
        priority=10,
        plugin_name=PLUGIN_INFO["name"],
        overwrite=True,
        metadata={"description": "Claude Code 完整伪装请求处理"},
    )
    register_response_interceptor(
        interceptor_id="claude_code_compat_response",
        callback=claude_code_compat_response_interceptor,
        priority=10,
        plugin_name=PLUGIN_INFO["name"],
        overwrite=True,
        metadata={"description": "Claude Code Layer 7 响应反向映射"},
    )


def teardown(manager):
    """插件卸载。"""
    unregister_request_interceptor("claude_code_compat_request")
    unregister_response_interceptor("claude_code_compat_response")
