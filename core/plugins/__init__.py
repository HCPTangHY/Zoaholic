"""
通用插件系统

支持多种扩展点：
- channels: 渠道适配器
- middlewares: 请求/响应中间件
- interceptors: 请求/响应拦截器和余额补充器
- processors: 自定义处理器

使用方式：
```python
from core.plugins import PluginManager, ExtensionPoint

# 获取插件管理器
manager = get_plugin_manager()

# 加载所有插件
manager.load_all()

# 获取特定扩展点的所有扩展
channel_extensions = manager.get_extensions("channels")

# 注册请求/响应拦截器和余额补充器
from core.plugins import register_request_interceptor, register_response_interceptor, register_balance_enricher

async def my_request_interceptor(request, engine, provider, api_key, url, headers, payload):
    # 修改请求
    return url, headers, payload

register_request_interceptor("my_interceptor", my_request_interceptor)
```
"""

from .extension import ExtensionPoint, Extension
from .registry import PluginRegistry
from .loader import PluginLoader, PluginInfo
from .manager import PluginManager, get_plugin_manager, init_plugin_manager
from .interceptors import (
    InterceptorRegistry,
    InterceptorEntry,
    get_interceptor_registry,
    register_inbound_interceptor,
    unregister_inbound_interceptor,
    apply_inbound_interceptors,
    register_request_interceptor,
    unregister_request_interceptor,
    register_response_interceptor,
    unregister_response_interceptor,
    # 修改原因：balance_enricher 是第三类插件拦截扩展点，需要从 core.plugins 顶层导出。
    # 修改方式：在现有 interceptors 导入列表中补充注册、注销和应用三个便捷函数。
    # 目的：让插件文件可以使用与 request/response 一致的导入路径。
    register_balance_enricher,
    unregister_balance_enricher,
    apply_request_interceptors,
    apply_response_interceptors,
    apply_balance_enrichers,
    # 插件参数解析工具
    parse_plugin_entry,
    parse_enabled_plugins,
    get_plugin_options,
    get_current_plugin_options,
    is_plugin_enabled,
)

__all__ = [
    # 扩展点
    "ExtensionPoint",
    "Extension",
    # 注册表
    "PluginRegistry",
    # 加载器
    "PluginLoader",
    "PluginInfo",
    # 管理器
    "PluginManager",
    "get_plugin_manager",
    "init_plugin_manager",
    # 拦截器系统
    "InterceptorRegistry",
    "InterceptorEntry",
    "get_interceptor_registry",
    "register_inbound_interceptor",
    "unregister_inbound_interceptor",
    "apply_inbound_interceptors",
    "register_request_interceptor",
    "unregister_request_interceptor",
    "register_response_interceptor",
    "unregister_response_interceptor",
    "register_balance_enricher",
    "unregister_balance_enricher",
    "apply_request_interceptors",
    "apply_response_interceptors",
    "apply_balance_enrichers",
    # 插件参数工具
    "parse_plugin_entry",
    "parse_enabled_plugins",
    "get_plugin_options",
    "get_current_plugin_options",
    "is_plugin_enabled",
]