"""
扩展点定义模块

定义扩展点（ExtensionPoint）和扩展（Extension）的基础类型。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from enum import Enum


class ExtensionPointType(str, Enum):
    """预定义的扩展点类型"""
    CHANNELS = "channels"           # 渠道适配器
    MIDDLEWARES = "middlewares"     # 请求/响应中间件
    HOOKS = "hooks"                 # 生命周期钩子
    PROCESSORS = "processors"       # 自定义处理器
    FORMATTERS = "formatters"       # 格式转换器
    VALIDATORS = "validators"       # 验证器
    CUSTOM = "custom"               # 自定义扩展点


@dataclass
class ExtensionPoint:
    """
    扩展点定义
    
    扩展点是插件系统的核心概念，定义了可以被插件扩展的功能点。
    
    Attributes:
        name: 扩展点唯一标识
        type: 扩展点类型
        description: 扩展点描述
        interface: 扩展必须实现的接口（函数签名或协议）
        required_methods: 扩展必须实现的方法列表
        optional_methods: 扩展可选实现的方法列表
        singleton: 是否只允许一个扩展（默认 False）
        priority_support: 是否支持优先级排序（默认 True）
    """
    name: str
    type: ExtensionPointType = ExtensionPointType.CUSTOM
    description: str = ""
    interface: Optional[Any] = None
    required_methods: List[str] = field(default_factory=list)
    optional_methods: List[str] = field(default_factory=list)
    singleton: bool = False
    priority_support: bool = True


@dataclass
class Extension:
    """
    扩展定义
    
    表示一个具体的扩展实现。
    
    Attributes:
        id: 扩展唯一标识
        extension_point: 所属扩展点名称
        implementation: 扩展的实现（可以是函数、类或对象）
        priority: 优先级（数值越小优先级越高，默认 100）
        enabled: 是否启用
        metadata: 扩展元数据
        plugin_name: 所属插件名称
    """
    id: str
    extension_point: str
    implementation: Any
    priority: int = 100
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)
    plugin_name: Optional[str] = None
    
    def __post_init__(self):
        if not self.metadata:
            self.metadata = {}


# 预定义的扩展点
BUILTIN_EXTENSION_POINTS = {
    ExtensionPointType.CHANNELS: ExtensionPoint(
        name="channels",
        type=ExtensionPointType.CHANNELS,
        description="渠道适配器扩展点，用于注册新的 API 渠道",
        required_methods=["register"],
        optional_methods=["unregister"],
    ),
    ExtensionPointType.MIDDLEWARES: ExtensionPoint(
        name="middlewares",
        type=ExtensionPointType.MIDDLEWARES,
        description="中间件扩展点，用于处理请求和响应",
        required_methods=["process_request", "process_response"],
        optional_methods=["on_error"],
        priority_support=True,
    ),
    ExtensionPointType.HOOKS: ExtensionPoint(
        name="hooks",
        type=ExtensionPointType.HOOKS,
        description="生命周期钩子扩展点",
        required_methods=[],
        optional_methods=[
            "on_startup",
            "on_shutdown",
            "before_request",
            "after_request",
            "on_error",
        ],
    ),
    ExtensionPointType.PROCESSORS: ExtensionPoint(
        name="processors",
        type=ExtensionPointType.PROCESSORS,
        description="自定义处理器扩展点",
        required_methods=["process"],
        optional_methods=["validate", "cleanup"],
    ),
    ExtensionPointType.FORMATTERS: ExtensionPoint(
        name="formatters",
        type=ExtensionPointType.FORMATTERS,
        description="格式转换器扩展点",
        required_methods=["format"],
        optional_methods=["parse"],
    ),
    ExtensionPointType.VALIDATORS: ExtensionPoint(
        name="validators",
        type=ExtensionPointType.VALIDATORS,
        description="验证器扩展点",
        required_methods=["validate"],
        optional_methods=[],
    ),
}