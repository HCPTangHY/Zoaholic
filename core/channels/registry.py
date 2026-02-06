from dataclasses import dataclass
from typing import Any, Awaitable, Callable, AsyncIterator, Dict, List, Optional

import httpx


# Type aliases for better readability
# - RequestAdapter: build (url, headers, payload) for a given provider/engine/request
# - StreamAdapter: handle streaming responses
# - ResponseAdapter: handle non-stream responses (仍然是 async generator)
# - ModelsAdapter: fetch models list for a given provider config
RequestAdapter = Callable[
    [Any, str, Dict[str, Any], Optional[str]],
    Awaitable[tuple[str, Dict[str, Any], Dict[str, Any]]],
]

StreamAdapter = Callable[
    [httpx.AsyncClient, str, Dict[str, Any], Dict[str, Any], str, str, int],
    AsyncIterator[Any],
]

ResponseAdapter = Callable[
    [httpx.AsyncClient, str, Dict[str, Any], Dict[str, Any], str, str, int],
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
    stream_adapter: Optional[StreamAdapter] = None
    response_adapter: Optional[ResponseAdapter] = None
    models_adapter: Optional[ModelsAdapter] = None
    # 透传模式下对 payload 做二次修饰（保持渠道特殊逻辑在渠道文件内）
    passthrough_payload_adapter: Optional[PassthroughPayloadAdapter] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于 API 响应"""
        return {
            "id": self.id,
            "type_name": self.type_name,
            "default_base_url": self.default_base_url,
            "auth_header": self.auth_header,
            "description": self.description,
            "has_passthrough_adapter": self.passthrough_adapter is not None,
            "has_models_adapter": self.models_adapter is not None,
        }


# 全局注册表: key 为 channel id(engine), value 为渠道定义
_REGISTRY: Dict[str, ChannelDefinition] = {}


def register_channel(
    id: str,
    type_name: str,
    default_base_url: Optional[str] = None,
    auth_header: Optional[str] = None,
    description: Optional[str] = None,
    request_adapter: Optional[RequestAdapter] = None,
    stream_adapter: Optional[StreamAdapter] = None,
    response_adapter: Optional[ResponseAdapter] = None,
    models_adapter: Optional[ModelsAdapter] = None,
    overwrite: bool = False,
    *,
    passthrough_adapter: Optional[RequestAdapter] = None,
    passthrough_payload_adapter: Optional[PassthroughPayloadAdapter] = None,
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

    _REGISTRY[id] = ChannelDefinition(
        id=id,
        type_name=type_name,
        default_base_url=default_base_url,
        auth_header=auth_header,
        description=description,
        request_adapter=request_adapter,
        passthrough_adapter=passthrough_adapter,
        passthrough_payload_adapter=passthrough_payload_adapter,
        stream_adapter=stream_adapter,
        response_adapter=response_adapter,
        models_adapter=models_adapter,
    )


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