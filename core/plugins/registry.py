"""
插件注册表模块

管理扩展点和扩展的注册与查询。
"""

from typing import Any, Callable, Dict, List, Optional, Set, Type
from collections import defaultdict

from .extension import ExtensionPoint, Extension, ExtensionPointType, BUILTIN_EXTENSION_POINTS


class PluginRegistry:
    """
    插件注册表
    
    统一管理所有扩展点和扩展。
    """
    
    def __init__(self):
        # 扩展点注册表: {extension_point_name: ExtensionPoint}
        self._extension_points: Dict[str, ExtensionPoint] = {}
        
        # 扩展注册表: {extension_point_name: {extension_id: Extension}}
        self._extensions: Dict[str, Dict[str, Extension]] = defaultdict(dict)
        
        # 注册内置扩展点
        for ep_type, ep in BUILTIN_EXTENSION_POINTS.items():
            self.register_extension_point(ep)
    
    # ==================== 扩展点管理 ====================
    
    def register_extension_point(self, extension_point: ExtensionPoint) -> None:
        """
        注册扩展点
        
        Args:
            extension_point: 扩展点定义
            
        Raises:
            ValueError: 如果扩展点已存在
        """
        if extension_point.name in self._extension_points:
            raise ValueError(f"Extension point '{extension_point.name}' already registered")
        
        self._extension_points[extension_point.name] = extension_point
    
    def get_extension_point(self, name: str) -> Optional[ExtensionPoint]:
        """获取扩展点定义"""
        return self._extension_points.get(name)
    
    def list_extension_points(self) -> List[ExtensionPoint]:
        """列出所有扩展点"""
        return list(self._extension_points.values())
    
    def has_extension_point(self, name: str) -> bool:
        """检查扩展点是否存在"""
        return name in self._extension_points
    
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
            
        Raises:
            ValueError: 如果扩展点不存在或扩展已存在
        """
        # 检查扩展点是否存在
        if extension_point not in self._extension_points:
            raise ValueError(f"Extension point '{extension_point}' not found")
        
        ep = self._extension_points[extension_point]
        
        # 检查是否为单例扩展点
        if ep.singleton and self._extensions[extension_point] and not overwrite:
            existing = list(self._extensions[extension_point].keys())[0]
            raise ValueError(
                f"Extension point '{extension_point}' is singleton, "
                f"already has extension '{existing}'"
            )
        
        # 检查扩展是否已存在
        if extension_id in self._extensions[extension_point] and not overwrite:
            raise ValueError(
                f"Extension '{extension_id}' already registered "
                f"for extension point '{extension_point}'"
            )
        
        # 验证实现是否满足接口要求
        self._validate_implementation(ep, implementation)
        
        # 创建扩展对象
        extension = Extension(
            id=extension_id,
            extension_point=extension_point,
            implementation=implementation,
            priority=priority,
            metadata=metadata or {},
            plugin_name=plugin_name,
        )
        
        self._extensions[extension_point][extension_id] = extension
        return extension
    
    def unregister_extension(self, extension_point: str, extension_id: str) -> bool:
        """
        注销扩展
        
        Args:
            extension_point: 扩展点名称
            extension_id: 扩展标识
            
        Returns:
            是否成功注销
        """
        if extension_point in self._extensions:
            if extension_id in self._extensions[extension_point]:
                del self._extensions[extension_point][extension_id]
                return True
        return False
    
    def get_extension(self, extension_point: str, extension_id: str) -> Optional[Extension]:
        """获取指定扩展"""
        return self._extensions.get(extension_point, {}).get(extension_id)
    
    def get_extensions(
        self,
        extension_point: str,
        enabled_only: bool = True,
        sorted_by_priority: bool = True,
    ) -> List[Extension]:
        """
        获取扩展点的所有扩展
        
        Args:
            extension_point: 扩展点名称
            enabled_only: 是否只返回启用的扩展
            sorted_by_priority: 是否按优先级排序
            
        Returns:
            扩展列表
        """
        extensions = list(self._extensions.get(extension_point, {}).values())
        
        if enabled_only:
            extensions = [e for e in extensions if e.enabled]
        
        if sorted_by_priority:
            extensions.sort(key=lambda e: e.priority)
        
        return extensions
    
    def get_implementations(
        self,
        extension_point: str,
        enabled_only: bool = True,
        sorted_by_priority: bool = True,
    ) -> List[Any]:
        """
        获取扩展点的所有实现
        
        Args:
            extension_point: 扩展点名称
            enabled_only: 是否只返回启用的扩展
            sorted_by_priority: 是否按优先级排序
            
        Returns:
            实现列表
        """
        extensions = self.get_extensions(extension_point, enabled_only, sorted_by_priority)
        return [e.implementation for e in extensions]
    
    def list_extensions(self, extension_point: Optional[str] = None) -> Dict[str, List[Extension]]:
        """
        列出扩展
        
        Args:
            extension_point: 可选，指定扩展点名称
            
        Returns:
            {扩展点名称: [扩展列表]}
        """
        if extension_point:
            return {extension_point: list(self._extensions.get(extension_point, {}).values())}
        
        return {
            ep: list(exts.values())
            for ep, exts in self._extensions.items()
        }
    
    def enable_extension(self, extension_point: str, extension_id: str) -> bool:
        """启用扩展"""
        ext = self.get_extension(extension_point, extension_id)
        if ext:
            ext.enabled = True
            return True
        return False
    
    def disable_extension(self, extension_point: str, extension_id: str) -> bool:
        """禁用扩展"""
        ext = self.get_extension(extension_point, extension_id)
        if ext:
            ext.enabled = False
            return True
        return False
    
    def set_extension_priority(
        self,
        extension_point: str,
        extension_id: str,
        priority: int
    ) -> bool:
        """设置扩展优先级"""
        ext = self.get_extension(extension_point, extension_id)
        if ext:
            ext.priority = priority
            return True
        return False
    
    # ==================== 辅助方法 ====================
    
    def _validate_implementation(self, ep: ExtensionPoint, impl: Any) -> None:
        """验证实现是否满足扩展点的接口要求"""
        for method in ep.required_methods:
            if not hasattr(impl, method) and not callable(impl):
                raise ValueError(
                    f"Implementation must have method '{method}' "
                    f"required by extension point '{ep.name}'"
                )
    
    def clear(self) -> None:
        """清空所有扩展（保留扩展点定义）"""
        self._extensions.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """获取注册表统计信息"""
        return {
            "extension_points": len(self._extension_points),
            "total_extensions": sum(len(exts) for exts in self._extensions.values()),
            "extensions_by_point": {
                ep: len(exts) for ep, exts in self._extensions.items()
            },
            "enabled_extensions": sum(
                len([e for e in exts.values() if e.enabled])
                for exts in self._extensions.values()
            ),
        }


# 全局注册表实例
_global_registry: Optional[PluginRegistry] = None


def get_registry() -> PluginRegistry:
    """获取全局注册表实例"""
    global _global_registry
    if _global_registry is None:
        _global_registry = PluginRegistry()
    return _global_registry


def reset_registry() -> None:
    """重置全局注册表（主要用于测试）"""
    global _global_registry
    _global_registry = None