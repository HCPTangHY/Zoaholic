"""
通用插件系统

支持多种扩展点：
- channels: 渠道适配器
- middlewares: 请求/响应中间件
- hooks: 生命周期钩子
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
```
"""

from .extension import ExtensionPoint, Extension
from .registry import PluginRegistry
from .loader import PluginLoader, PluginInfo
from .manager import PluginManager, get_plugin_manager, init_plugin_manager

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
]