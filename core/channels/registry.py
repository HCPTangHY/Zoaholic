from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable, AsyncIterator, Dict, List, Optional, Tuple

import httpx


logger = logging.getLogger(__name__)


# Type aliases for better readability
# - RequestAdapter: build (url, headers, payload) for a given provider/engine/request
# - StreamAdapter: handle streaming responses
# - ResponseAdapter: handle non-stream responses (仍然是 async generator)
# - ModelsAdapter: fetch models list for a given provider config
# 修改原因：部分部署环境的 python3 仍会执行类型别名表达式，内建 tuple[...] 在旧版本 Python 中不可下标。
# 修改方式：RequestAdapter 返回类型使用 typing.Tuple，避免导入 registry.py 时触发 TypeError。
# 目的：让注册表在用户要求的 python3 验证命令下也能稳定导入。
RequestAdapter = Callable[
    [Any, str, Dict[str, Any], Optional[str]],
    Awaitable[Tuple[str, Dict[str, Any], Dict[str, Any]]],
]

StreamAdapter = Callable[
    [httpx.AsyncClient, str, Dict[str, Any], Dict[str, Any], str, str, int],
    AsyncIterator[Any],
]

ResponseAdapter = Callable[
    [httpx.AsyncClient, str, Dict[str, Any], Dict[str, Any], str, str, int],
    AsyncIterator[Any],
]

# PassthroughStreamAdapter / PassthroughResponseAdapter: 透传模式下的渠道自定义上游响应处理器。
# 修改原因：AWS Bedrock 流式响应是二进制事件流，通用 SSE 透传处理器无法解析。
# 修改方式：在注册表层增加两个可选钩子，让特殊渠道只替换透传响应读取部分。
# 目的：保持普通透传渠道沿用默认处理，同时允许 AWS 将 Bedrock 事件流转换为 Claude SSE。
PassthroughStreamAdapter = Callable[
    [httpx.AsyncClient, str, Dict[str, Any], Dict[str, Any], str, int],
    AsyncIterator[Any],
]

PassthroughResponseAdapter = Callable[
    [httpx.AsyncClient, str, Dict[str, Any], Dict[str, Any], str, int],
    AsyncIterator[Any],
]

# ModelsAdapter: 根据 provider 配置获取模型列表
# 参数: (client, provider_config) -> 返回模型 ID 列表
ModelsAdapter = Callable[
    [httpx.AsyncClient, Dict[str, Any]],
    Awaitable[List[str]],
]

# PassthroughPayloadAdapter: 透传模式下对 native payload 做渠道级修饰（例如 system_prompt 注入）
PassthroughPayloadAdapter = Callable[
    [Dict[str, Any], Dict[str, Any], Any, str, Dict[str, Any], Optional[str]],
    Awaitable[Dict[str, Any]],
]


@dataclass
class ChannelDefinition:
    """
    通用渠道定义:
    - id: 渠道唯一标识(通常对应 engine, 如 "openai" / "gemini" / "vertex-gemini")
    - type_name: 渠道类型名(如 "openai" / "gemini" 等, 用于分类/展示)
    - default_base_url: 默认的 Base URL (可选, 用于前端自动填充)
    - auth_header: 认证头格式 (可选, 如 "Bearer {api_key}" 或 "x-api-key: {api_key}")
    - description: 渠道描述 (可选, 用于前端展示)
    - request_adapter: 构造下游请求(url, headers, payload) 的适配器
    - stream_adapter: 处理流式响应的适配器
    - response_adapter: 处理非流式响应的适配器 (返回 async generator)
    - models_adapter: 获取模型列表的适配器 (可选, 每个渠道可以有自己的实现)
    """

    id: str
    type_name: str
    default_base_url: Optional[str] = None
    auth_header: Optional[str] = None
    description: Optional[str] = None
    request_adapter: Optional[RequestAdapter] = None
    # 透传时构建 url/headers 的适配器（默认复用 request_adapter）
    passthrough_adapter: Optional[RequestAdapter] = None
    # 修改原因：部分渠道的透传响应格式与通用 SSE/JSON 读取器不兼容。
    # 修改方式：把透传流式和非流式响应处理器作为可选注册字段保存。
    # 目的：让 AWS Bedrock 可以在透传路径中解析自己的事件流，而不影响其他渠道。
    passthrough_stream_adapter: Optional[PassthroughStreamAdapter] = None
    passthrough_response_adapter: Optional[PassthroughResponseAdapter] = None
    stream_adapter: Optional[StreamAdapter] = None
    response_adapter: Optional[ResponseAdapter] = None
    models_adapter: Optional[ModelsAdapter] = None
    # 透传模式下对 payload 做二次修饰（保持渠道特殊逻辑在渠道文件内）
    passthrough_payload_adapter: Optional[PassthroughPayloadAdapter] = None
    supports_documents: bool = False
    supports_audio: bool = False
    default_token_url: Optional[str] = None
    # 修改原因：余额查询路由需要区分普通 API Key 渠道和 OAuth 凭据渠道。
    # 修改方式：在渠道定义上保存只读布尔标记，由具体渠道注册时声明。
    # 目的：让后端路由和前端管理页都通过统一注册表识别 OAuth 引擎。
    is_oauth: bool = False
    # 修改原因：渠道/插件需要能注册任意前端渲染代码，不应为每种 UI 新增专有字段。
    # 修改方式：新增通用 ui_slots 字典，key 为插槽名（如 quota_display、key_detail），value 为内联 JS 字符串。
    # 目的：让渠道在 register_channel 时一次性声明所有前端插槽，以后加新插槽只需往 dict 里写新 key。
    ui_slots: Optional[Dict[str, str]] = None
    # 修改原因：OAuth provider 注册需要从 main.py 的硬编码迁移到渠道注册表，插件渠道也要能声明自己的 provider。
    # 修改方式：在 ChannelDefinition 上保存运行时使用的 OAuthProvider 实例，但不在 to_dict API 输出中暴露。
    # 目的：启动时扫描注册表即可统一注册内置和外置 OAuth 渠道，同时避免把 provider 对象返回给前端。
    oauth_provider: Any = None
    source: str = "plugin"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于 API 响应"""
        from core.ui_slots.registry import resolve_slots_for_engine

        # 修改原因：ui_slots 已从渠道私有字段扩展为渠道和插件共同贡献，且插件门控 slot 也必须返回给前端判断。
        # 修改方式：按当前渠道 id 和认证类型解析基础 channel_slots，再由 resolver 为 enabled_plugin 贡献标注 requires_plugin。
        # 目的：让 /v1/channels 输出完整 resolved slots，同时保留 provider 级插件开关条件供管理端使用。
        resolved_ui_slots = resolve_slots_for_engine(
            self.id,
            auth_type="oauth" if self.is_oauth else "api_key",
            channel_slots=self.ui_slots,
        )

        return {
            "id": self.id,
            "type_name": self.type_name,
            "default_base_url": self.default_base_url,
            "default_token_url": self.default_token_url,
            "auth_header": self.auth_header,
            "description": self.description,
            "has_passthrough_adapter": self.passthrough_adapter is not None,
            # 修改原因：管理端需要能观察渠道是否覆盖了默认透传响应处理。
            # 修改方式：在字典输出中加入两个只读布尔标记。
            # 目的：排查 AWS Claude 透传时可确认实际使用了自定义 Bedrock 处理器。
            "has_passthrough_stream_adapter": self.passthrough_stream_adapter is not None,
            "has_passthrough_response_adapter": self.passthrough_response_adapter is not None,
            "has_models_adapter": self.models_adapter is not None,
            "supports_documents": self.supports_documents,
            "supports_audio": self.supports_audio,
            # 修改原因：管理端需要知道某个 engine 是否应走 OAuth 账号 UI 和余额分流。
            # 修改方式：把 ChannelDefinition.is_oauth 输出为只读字段。
            # 目的：前端无需继续只依赖硬编码引擎列表即可识别 OAuth 渠道。
            "is_oauth": self.is_oauth,
            # 修改原因：前端需要拿到渠道基础 slot、全局 UI slot 贡献，以及插件门控 slot 的生效条件。
            # 修改方式：输出 resolve_slots_for_engine 解析得到的 resolved_ui_slots，而不是原始 self.ui_slots。
            # 目的：让插件和渠道都能通过同一个渠道元数据接口把插槽脚本及 requires_plugin 条件交给前端。
            "ui_slots": resolved_ui_slots,
            "source": self.source,
        }


# 全局注册表: key 为 channel id(engine), value 为渠道定义
_REGISTRY: Dict[str, ChannelDefinition] = {}

# 修改原因：OAuthManager 需要在渠道注册时接收 oauth_provider，但 registry 不能直接导入 OAuthManager 以免循环依赖。
# 修改方式：保存一个运行时注入的注册回调，由 OAuthManager 初始化时安装，渠道注册表只在有 provider 时调用回调。
# 目的：让内置渠道和插件渠道声明 oauth_provider 后可自动注册，同时保留 main.py 中的兜底扫描逻辑。
_OAUTH_PROVIDER_REGISTRAR: Optional[Callable[[str, Any], None]] = None


def set_oauth_provider_registrar(
    registrar: Optional[Callable[[str, Any], None]],
    *,
    replay_existing: bool = False,
) -> None:
    """设置运行时 OAuth provider 注册回调。"""
    global _OAUTH_PROVIDER_REGISTRAR
    _OAUTH_PROVIDER_REGISTRAR = registrar
    # 修改原因：OAuthManager 可能在部分渠道已经注册后才初始化，不能只处理后续新增渠道。
    # 修改方式：调用方传 replay_existing=True 时遍历当前注册表，并复用统一的安全注册函数。
    # 目的：保证启动顺序不影响 OAuth provider 自动注册结果。
    if replay_existing and registrar is not None:
        for channel_id, channel_def in list(_REGISTRY.items()):
            _register_oauth_provider_if_possible(channel_id, channel_def.oauth_provider)


def _register_oauth_provider_if_possible(channel_id: str, oauth_provider: Any) -> None:
    """在 OAuthManager 已接入时，把渠道 provider 同步注册过去。"""
    # 修改原因：普通渠道没有 oauth_provider，且 OAuthManager 初始化前也没有可用回调，这两种情况都应安全跳过。
    # 修改方式：只有 provider 和 registrar 同时存在时才调用回调，并捕获异常写入日志。
    # 目的：避免单个渠道 provider 注册失败中断整体渠道注册流程。
    if oauth_provider is None or _OAUTH_PROVIDER_REGISTRAR is None:
        return
    try:
        _OAUTH_PROVIDER_REGISTRAR(channel_id, oauth_provider)
    except Exception:
        logger.exception("Failed to auto-register OAuth provider for channel %s", channel_id)


def register_channel(
    id: str,
    type_name: str,
    default_base_url: Optional[str] = None,
    default_token_url: Optional[str] = None,
    auth_header: Optional[str] = None,
    description: Optional[str] = None,
    request_adapter: Optional[RequestAdapter] = None,
    stream_adapter: Optional[StreamAdapter] = None,
    response_adapter: Optional[ResponseAdapter] = None,
    models_adapter: Optional[ModelsAdapter] = None,
    overwrite: bool = False,
    *,
    passthrough_adapter: Optional[RequestAdapter] = None,
    passthrough_stream_adapter: Optional[PassthroughStreamAdapter] = None,
    passthrough_response_adapter: Optional[PassthroughResponseAdapter] = None,
    supports_documents: bool = False,
    supports_audio: bool = False,
    passthrough_payload_adapter: Optional[PassthroughPayloadAdapter] = None,
    # 修改原因：OAuth 渠道的凭据和余额查询都由 OAuthManager 处理，和普通 API Key 渠道不同。
    # 修改方式：register_channel 增加可选 is_oauth 参数，默认保持 False 以兼容既有渠道。
    # 目的：具体渠道只需在注册时声明一次，后续路由和前端都可读取同一标记。
    is_oauth: bool = False,
    # 修改原因：渠道注册时需要一并声明前端各插槽的渲染代码。
    # 修改方式：新增可选 ui_slots 参数（dict[str, str]），透传至 ChannelDefinition。
    # 目的：让渠道在单一 register_channel 调用中完成后端适配器和所有前端插槽 UI 的注册。
    ui_slots: Optional[Dict[str, str]] = None,
    # 修改原因：OAuth provider 实例应由渠道自身在 register_channel 时声明，main.py 不应维护渠道清单。
    # 修改方式：新增可选 oauth_provider 参数；传入 provider 时会自动把渠道标记为 OAuth 渠道。
    # 目的：让内置渠道和插件渠道都通过同一个注册入口完成 OAuth provider 暴露。
    oauth_provider: Any = None,
    source: str = "plugin",
) -> None:
    """
    注册一个渠道, 供 core.request / core.response 统一调度使用。
    
    Args:
        id: 渠道唯一标识
        type_name: 渠道类型名
        default_base_url: 默认的 Base URL
        auth_header: 认证头格式
        description: 渠道描述
        request_adapter: 请求适配器
        stream_adapter: 流式响应适配器
        response_adapter: 非流式响应适配器
        models_adapter: 模型列表适配器
        overwrite: 是否覆盖已存在的渠道（用于插件热重载）
    """
    if id in _REGISTRY and not overwrite:
        raise ValueError(f"Channel with id={id!r} already registered")

    # 修改原因：调用方传入 oauth_provider 时已经明确该渠道走 OAuth 凭据路径，不能要求重复传 is_oauth=True。
    # 修改方式：注册前根据 oauth_provider 是否存在自动提升 is_oauth 标记，同时保留显式 is_oauth=True 的旧调用。
    # 目的：减少渠道和插件注册时的重复参数，并避免 provider 已声明但余额路由仍按普通 Key 渠道处理。
    is_oauth = is_oauth or (oauth_provider is not None)

    _REGISTRY[id] = ChannelDefinition(
        id=id,
        type_name=type_name,
        default_base_url=default_base_url,
        default_token_url=default_token_url,
        auth_header=auth_header,
        description=description,
        request_adapter=request_adapter,
        passthrough_adapter=passthrough_adapter,
        # 修改原因：register_channel 新增的透传响应钩子必须随渠道定义一起落库。
        # 修改方式：把传入的自定义透传流式/非流式适配器写入 ChannelDefinition。
        # 目的：handler 在透传分发时可以发现并调用渠道专用处理器。
        passthrough_stream_adapter=passthrough_stream_adapter,
        passthrough_response_adapter=passthrough_response_adapter,
        passthrough_payload_adapter=passthrough_payload_adapter,
        stream_adapter=stream_adapter,
        response_adapter=response_adapter,
        models_adapter=models_adapter,
        supports_documents=supports_documents,
        supports_audio=supports_audio,
        # 修改原因：OAuth 标记是渠道定义的一部分，必须随注册参数一起保存。
        # 修改方式：把 register_channel 的 is_oauth 参数写入 ChannelDefinition。
        # 目的：余额路由可以从注册表稳定判断是否调用 OAuthManager.fetch_quota。
        is_oauth=is_oauth,
        # 修改原因：前端插槽脚本是渠道定义的一部分，需要随注册参数一起保存。
        # 修改方式：把 register_channel 的 ui_slots 参数写入 ChannelDefinition。
        # 目的：让渠道元数据接口能把各插槽脚本交给前端动态加载。
        ui_slots=ui_slots,
        # 修改原因：main.py 需要通过注册表发现每个渠道声明的 OAuthProvider 实例。
        # 修改方式：把 register_channel 的 oauth_provider 参数写入 ChannelDefinition，但不改变 to_dict 输出。
        # 目的：消除启动期硬编码注册清单，并让外置插件可以复用同一条注册路径。
        oauth_provider=oauth_provider,
        source=source,
    )
    # 修改原因：渠道可能在 OAuthManager 初始化之后由插件或热重载流程注册，不能只依赖启动期扫描。
    # 修改方式：写入 ChannelDefinition 后立即尝试把 oauth_provider 交给已安装的回调。
    # 目的：让新注册渠道的 OAuth provider 自动进入 OAuthManager，同时保持无回调时静默兼容。
    _register_oauth_provider_if_possible(id, oauth_provider)


def unregister_channel(id: str) -> bool:
    """
    注销一个渠道。
    
    Args:
        id: 渠道唯一标识
        
    Returns:
        是否成功注销（False 表示渠道不存在）
    """
    if id in _REGISTRY:
        del _REGISTRY[id]
        return True
    return False


def get_channel(id: str) -> Optional[ChannelDefinition]:
    """
    按 id(engine) 获取渠道定义, 若未注册则返回 None。
    """
    return _REGISTRY.get(id)


def get_all_channels() -> Dict[str, ChannelDefinition]:
    """返回当前渠道注册表的浅拷贝。"""
    # 修改原因：OAuth provider 自动注册需要按渠道 ID 遍历完整注册表，而 list_channels 会丢失字典 key。
    # 修改方式：提供只读语义的浅拷贝 getter，不暴露内部 _REGISTRY 对象供外部直接修改。
    # 目的：main.py 和测试都能稳定枚举渠道定义，同时降低外部误改全局注册表的风险。
    return dict(_REGISTRY)


def list_channels() -> List[ChannelDefinition]:
    """
    返回当前已注册的所有渠道定义列表。
    """
    return list(_REGISTRY.values())


def list_channel_ids() -> List[str]:
    """
    返回当前已注册的所有渠道 ID 列表。
    """
    return list(_REGISTRY.keys())