"""
方言（Dialect）注册中心

Dialect 用于描述“外部 API 输入/输出格式”（OpenAI / Gemini / Claude 等），
负责 native <-> Canonical 的转换与透传检测。

此模块仅提供注册表与基础类型定义，不涉及具体格式实现。
"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, AsyncIterator, Union

from core.models import RequestModel

# Type aliases
# ParseRequest: native request -> canonical RequestModel
ParseRequest = Callable[
    [Dict[str, Any], Dict[str, str], Dict[str, str]],
    Awaitable[RequestModel],
]

# RenderResponse: canonical json -> native json
RenderResponse = Callable[
    [Dict[str, Any], str],
    Awaitable[Dict[str, Any]],
]

# RenderStream: canonical SSE chunk -> native SSE chunk
RenderStream = Callable[[str], Awaitable[str]]

# DetectPassthrough: (dialect_id, target_engine) -> bool
DetectPassthrough = Callable[[str, str], bool]

# SanitizeResponse: (native_chunk, request_model, original_model) -> sanitized_chunk
# 透传响应净化函数：替换模型名、过滤敏感信息
SanitizeResponse = Callable[[str, str, str], Awaitable[str]]

# ExtractToken: (request) -> Optional[str]
# 从请求中提取认证 token 的函数（方言自定义认证方式）
ExtractToken = Callable[[Any], Awaitable[Optional[str]]]

# EndpointHandler: 自定义端点处理函数
EndpointHandler = Callable[..., Awaitable[Any]]

# ParseUsage: (native_data) -> Optional[Dict[str, int]]
# 从原生响应（dict）或 SSE 行（str）中提取 usage
# 返回: {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
ParseUsage = Callable[[Union[Dict[str, Any], str]], Optional[Dict[str, int]]]


@dataclass
class EndpointDefinition:
    """
    端点定义：描述一个 HTTP 端点

    Attributes:
        path: 路由路径，支持 FastAPI 路径参数语法，如 "/models/{model}:generateContent"
        methods: HTTP 方法列表，默认 ["POST"]
        prefix: 路由前缀，如 "/v1beta"
        tags: OpenAPI tags，用于文档分组
        handler: 自定义处理函数（可选，不提供则使用通用处理函数）
        summary: 端点摘要（用于 OpenAPI 文档）
        description: 端点描述（用于 OpenAPI 文档）
    """

    path: str
    methods: List[str] = field(default_factory=lambda: ["POST"])
    prefix: str = ""
    tags: Optional[List[str]] = None
    handler: Optional[EndpointHandler] = None
    summary: Optional[str] = None
    description: Optional[str] = None

    @property
    def full_path(self) -> str:
        """返回完整路径（前缀 + 路径）"""
        return f"{self.prefix}{self.path}"


@dataclass
class DialectDefinition:
    """
    方言定义：描述一种外部 API 格式

    Attributes:
        id: 方言唯一标识 (openai, gemini, claude, ...)
        name: 显示名称
        description: 描述
        parse_request: 将原生请求转为 Canonical 的函数
        render_response: 将 Canonical 响应转为原生格式的函数
        render_stream: 将 Canonical SSE 流转为原生流格式的函数
        detect_passthrough: 检测是否可透传的函数（宽松模式：仅格式匹配）
        target_engine: 该方言对应的上游 engine（用于透传匹配）
        sanitize_response: 透传响应净化函数（替换模型名、过滤敏感信息）
        extract_token: 从请求中提取认证 token 的函数（可选，用于自定义认证方式）
        endpoints: 端点定义列表，用于自动注册路由
    """

    id: str
    name: str
    description: Optional[str] = None

    parse_request: Optional[ParseRequest] = None
    render_response: Optional[RenderResponse] = None
    render_stream: Optional[RenderStream] = None
    detect_passthrough: Optional[DetectPassthrough] = None
    target_engine: Optional[str] = None
    sanitize_response: Optional[SanitizeResponse] = None
    extract_token: Optional[ExtractToken] = None
    parse_usage: Optional[ParseUsage] = None

    # 端点定义：用于自动路由注册
    endpoints: List[EndpointDefinition] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典，用于 API/调试输出"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "has_parse_request": self.parse_request is not None,
            "has_render_response": self.render_response is not None,
            "has_render_stream": self.render_stream is not None,
            "target_engine": self.target_engine,
            "endpoints": [ep.full_path for ep in self.endpoints],
        }


# 全局注册表
_DIALECT_REGISTRY: Dict[str, DialectDefinition] = {}


def register_dialect(dialect: DialectDefinition, overwrite: bool = False) -> None:
    """注册方言定义"""
    if dialect.id in _DIALECT_REGISTRY and not overwrite:
        raise ValueError(f"Dialect with id={dialect.id!r} already registered")
    _DIALECT_REGISTRY[dialect.id] = dialect


def unregister_dialect(dialect_id: str) -> bool:
    """注销方言定义"""
    if dialect_id in _DIALECT_REGISTRY:
        del _DIALECT_REGISTRY[dialect_id]
        return True
    return False


def get_dialect(dialect_id: str) -> Optional[DialectDefinition]:
    """获取方言定义"""
    return _DIALECT_REGISTRY.get(dialect_id)


def list_dialects() -> List[DialectDefinition]:
    """列出所有已注册方言"""
    return list(_DIALECT_REGISTRY.values())


def list_dialect_ids() -> List[str]:
    """列出所有已注册方言 ID"""
    return list(_DIALECT_REGISTRY.keys())