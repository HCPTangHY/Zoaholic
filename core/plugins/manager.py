"""
插件管理器模块

提供插件系统的统一接口，整合加载器和注册表的功能。
"""

from typing import Any, Callable, Dict, List, Optional, Set, Type
from pathlib import Path

from ..log_config import logger
from .extension import ExtensionPoint, Extension, ExtensionPointType
from .registry import PluginRegistry, get_registry
from .loader import PluginLoader, PluginInfo


class PluginManager:
    """
    插件管理器
    
    统一管理插件的加载、注册、卸载等操作。
    
    使用示例:
    ```python
    # 获取管理器
    manager = get_plugin_manager()
    
    # 加载所有插件
    manager.load_all()
    
    # 注册扩展（通常由插件自动完成）
    manager.register_extension(
        extension_point="channels",
        extension_id="my_channel",
        implementation=my_channel_adapter
    )
    
    # 获取扩展
    channels = manager.get_extensions("channels")
    ```
    """
    
    def __init__(
        self,
        plugin_dirs: Optional[List[str]] = None,
        auto_load: bool = False,
    ):
        """
        初始化插件管理器
        
        Args:
            plugin_dirs: 插件目录列表
            auto_load: 是否自动加载插件
        """
        self._loader = PluginLoader(plugin_dirs)
        self._registry = get_registry()
        self._initialized = False
        self._hooks: Dict[str, List[Callable]] = {
            "before_load": [],
            "after_load": [],
            "before_unload": [],
            "after_unload": [],
            "on_error": [],
        }
        
        if auto_load:
            self.load_all()
    
    @property
    def loader(self) -> PluginLoader:
        """获取加载器实例"""
        return self._loader
    
    @property
    def registry(self) -> PluginRegistry:
        """获取注册表实例"""
        return self._registry
    
    @property
    def plugins(self) -> Dict[str, PluginInfo]:
        """获取所有已加载的插件"""
        return self._loader.plugins
    
    # ==================== 钩子管理 ====================
    
    def add_hook(self, hook_name: str, callback: Callable) -> None:
        """添加钩子回调"""
        if hook_name in self._hooks:
            self._hooks[hook_name].append(callback)
    
    def remove_hook(self, hook_name: str, callback: Callable) -> None:
        """移除钩子回调"""
        if hook_name in self._hooks and callback in self._hooks[hook_name]:
            self._hooks[hook_name].remove(callback)
    
    def _trigger_hook(self, hook_name: str, *args, **kwargs) -> None:
        """触发钩子"""
        for callback in self._hooks.get(hook_name, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"Hook {hook_name} callback error: {e}")
    
    # ==================== 插件加载 ====================
    
    def load_all(self, activate: bool = True) -> Dict[str, List[PluginInfo]]:
        """
        加载所有插件
        
        Args:
            activate: 是否激活（调用 setup）插件
            
        Returns:
            按来源分组的插件信息
        """
        self._trigger_hook("before_load")
        
        result = self._loader.load_all()
        
        if activate:
            for source, plugins in result.items():
                for plugin in plugins:
                    if plugin.enabled:
                        self._activate_plugin(plugin)
        
        self._initialized = True
        self._trigger_hook("after_load", result)
        
        return result
    
    def load_plugin(self, path_or_module: str, activate: bool = True, overwrite: bool = True) -> Optional[PluginInfo]:
        """
        加载单个插件
        
        Args:
            path_or_module: 文件路径或模块路径
            activate: 是否激活插件
            overwrite: 是否覆盖已存在的同名插件（默认为 True，支持热插拔覆写）
            
        Returns:
            PluginInfo 或 None
        """
        self._trigger_hook("before_load", path_or_module)
        
        # 判断是文件路径还是模块路径
        if path_or_module.endswith(".py") or "/" in path_or_module or "\\" in path_or_module:
            info = self._loader.load_from_file(path_or_module, overwrite=overwrite)
        else:
            info = self._loader.load_from_module(path_or_module, overwrite=overwrite)
        
        if info and info.enabled and activate:
            self._activate_plugin(info)
        
        self._trigger_hook("after_load", info)
        
        return info
    
    def unload_plugin(self, plugin_name: str, deactivate: bool = True) -> bool:
        """
        卸载插件
        
        Args:
            plugin_name: 插件名称
            deactivate: 是否停用（调用 teardown）插件
            
        Returns:
            是否成功卸载
        """
        self._trigger_hook("before_unload", plugin_name)
        
        info = self._loader.get_plugin(plugin_name)
        if info and deactivate:
            self._deactivate_plugin(info)
        
        result = self._loader.unload_plugin(plugin_name)
        
        self._trigger_hook("after_unload", plugin_name, result)
        
        return result
    
    def reload_plugin(self, plugin_name: str) -> Optional[PluginInfo]:
        """
        重新加载插件
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            PluginInfo 或 None
        """
        info = self._loader.get_plugin(plugin_name)
        if info:
            self._deactivate_plugin(info)
        
        new_info = self._loader.reload_plugin(plugin_name)
        
        if new_info and new_info.enabled:
            self._activate_plugin(new_info)
        
        return new_info
    
    def _activate_plugin(self, info: PluginInfo) -> None:
        """激活插件（调用 setup）"""
        if info.module and hasattr(info.module, "setup"):
            try:
                info.module.setup(self)
                logger.debug(f"Activated plugin: {info.name}")
            except Exception as e:
                logger.error(f"Failed to activate plugin {info.name}: {e}")
                self._trigger_hook("on_error", info, e)
    
    def _deactivate_plugin(self, info: PluginInfo) -> None:
        """停用插件（调用 teardown）"""
        if info.module and hasattr(info.module, "teardown"):
            try:
                info.module.teardown(self)
                logger.debug(f"Deactivated plugin: {info.name}")
            except Exception as e:
                logger.error(f"Failed to deactivate plugin {info.name}: {e}")
                self._trigger_hook("on_error", info, e)
        
        # 注销该插件的所有扩展
        for ep_name, extensions in self._registry.list_extensions().items():
            for ext in extensions:
                if ext.plugin_name == info.name:
                    self._registry.unregister_extension(ep_name, ext.id)
    
    # ==================== 扩展点管理 ====================
    
    def register_extension_point(self, extension_point: ExtensionPoint) -> None:
        """注册扩展点"""
        self._registry.register_extension_point(extension_point)
    
    def get_extension_point(self, name: str) -> Optional[ExtensionPoint]:
        """获取扩展点"""
        return self._registry.get_extension_point(name)
    
    def list_extension_points(self) -> List[ExtensionPoint]:
        """列出所有扩展点"""
        return self._registry.list_extension_points()
    
    # ==================== 扩展管理 ====================
    
    def register_extension(
        self,
        extension_point: str,
        extension_id: str,
        implementation: Any,
        priority: int = 100,
        metadata: Optional[Dict[str, Any]] = None,
        plugin_name: Optional[str] = None,
        overwrite: bool = False,
    ) -> Extension:
        """
        注册扩展
        
        Args:
            extension_point: 扩展点名称
            extension_id: 扩展唯一标识
            implementation: 扩展实现
            priority: 优先级
            metadata: 元数据
            plugin_name: 所属插件名称
            overwrite: 是否覆盖已存在的扩展
            
        Returns:
            注册的 Extension 对象
        """
        return self._registry.register_extension(
            extension_point=extension_point,
            extension_id=extension_id,
            implementation=implementation,
            priority=priority,
            metadata=metadata,
            plugin_name=plugin_name,
            overwrite=overwrite,
        )
    
    def unregister_extension(self, extension_point: str, extension_id: str) -> bool:
        """注销扩展"""
        return self._registry.unregister_extension(extension_point, extension_id)
    
    def get_extension(self, extension_point: str, extension_id: str) -> Optional[Extension]:
        """获取扩展"""
        return self._registry.get_extension(extension_point, extension_id)
    
    def get_extensions(
        self,
        extension_point: str,
        enabled_only: bool = True,
        sorted_by_priority: bool = True,
    ) -> List[Extension]:
        """获取扩展点的所有扩展"""
        return self._registry.get_extensions(extension_point, enabled_only, sorted_by_priority)
    
    def get_implementations(
        self,
        extension_point: str,
        enabled_only: bool = True,
        sorted_by_priority: bool = True,
    ) -> List[Any]:
        """获取扩展点的所有实现"""
        return self._registry.get_implementations(extension_point, enabled_only, sorted_by_priority)
    
    def enable_extension(self, extension_point: str, extension_id: str) -> bool:
        """启用扩展"""
        return self._registry.enable_extension(extension_point, extension_id)
    
    def disable_extension(self, extension_point: str, extension_id: str) -> bool:
        """禁用扩展"""
        return self._registry.disable_extension(extension_point, extension_id)
    
    # ==================== 便捷方法 ====================
    
    def get_channel_extensions(self) -> List[Extension]:
        """获取所有渠道扩展"""
        return self.get_extensions(ExtensionPointType.CHANNELS.value)
    
    def get_middleware_extensions(self) -> List[Extension]:
        """获取所有中间件扩展"""
        return self.get_extensions(ExtensionPointType.MIDDLEWARES.value)
    
    def get_hook_extensions(self) -> List[Extension]:
        """获取所有钩子扩展"""
        return self.get_extensions(ExtensionPointType.HOOKS.value)
    
    # ==================== 状态查询 ====================
    
    def get_status(self) -> Dict[str, Any]:
        """
        获取插件系统状态
        
        Returns:
            插件系统状态信息
        """
        return {
            "initialized": self._initialized,
            "loader": self._loader.get_status(),
            "registry": self._registry.get_stats(),
        }
    
    def is_initialized(self) -> bool:
        """检查是否已初始化"""
        return self._initialized


# 全局插件管理器实例
_plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """获取全局插件管理器实例"""
    global _plugin_manager
    if _plugin_manager is None:
        _plugin_manager = PluginManager()
    return _plugin_manager


def init_plugin_manager(
    plugin_dirs: Optional[List[str]] = None,
    auto_load: bool = False,
) -> PluginManager:
    """
    初始化全局插件管理器
    
    Args:
        plugin_dirs: 插件目录列表
        auto_load: 是否自动加载插件
        
    Returns:
        初始化后的 PluginManager 实例
    """
    global _plugin_manager
    _plugin_manager = PluginManager(plugin_dirs, auto_load)
    return _plugin_manager


def reset_plugin_manager() -> None:
    """重置全局插件管理器（主要用于测试）"""
    global _plugin_manager
    _plugin_manager = None