"""
插件加载器模块

支持以下插件加载方式：
1. 目录扫描：自动加载 plugins/ 目录下的 Python 脚本
2. Entry Points：加载通过 pip 安装的插件包
3. 动态加载：运行时通过 API 加载插件
"""

import os
import sys
import importlib
import importlib.util
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from datetime import datetime

from ..log_config import logger


@dataclass
class PluginInfo:
    """插件信息"""
    name: str
    path: str
    source: str  # "directory" | "entry_point" | "dynamic"
    loaded_at: datetime = field(default_factory=datetime.now)
    version: str = "unknown"
    description: str = ""
    author: str = ""
    enabled: bool = True
    module: Any = None
    error: Optional[str] = None
    # 插件提供的扩展
    extensions: List[str] = field(default_factory=list)
    # 插件依赖
    dependencies: List[str] = field(default_factory=list)
    # 插件元数据
    metadata: Dict[str, Any] = field(default_factory=dict)


class PluginLoader:
    """
    插件加载器
    
    负责从各种来源加载插件模块。
    """
    
    # 默认插件目录（相对于项目根目录）
    DEFAULT_PLUGIN_DIR = "plugins"
    
    # Entry point 组名
    ENTRY_POINT_GROUP = "zoaholic.plugins"
    
    def __init__(self, plugin_dirs: Optional[List[str]] = None):
        """
        初始化插件加载器
        
        Args:
            plugin_dirs: 插件目录列表，默认为 ["plugins"]
        """
        self._plugins: Dict[str, PluginInfo] = {}
        self._plugin_dirs: List[Path] = []
        
        # 设置插件目录
        if plugin_dirs:
            for d in plugin_dirs:
                self._plugin_dirs.append(Path(d))
        else:
            # 默认插件目录
            base_dir = Path(__file__).parent.parent.parent
            self._plugin_dirs.append(base_dir / self.DEFAULT_PLUGIN_DIR)
    
    @property
    def plugins(self) -> Dict[str, PluginInfo]:
        """获取所有已加载的插件信息"""
        return self._plugins.copy()
    
    @property
    def plugin_dirs(self) -> List[Path]:
        """获取插件目录列表"""
        return self._plugin_dirs.copy()
    
    def _ensure_plugin_dirs(self) -> None:
        """确保插件目录存在"""
        for plugin_dir in self._plugin_dirs:
            if not plugin_dir.exists():
                plugin_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"Created plugin directory: {plugin_dir}")
                
                # 创建 __init__.py
                init_file = plugin_dir / "__init__.py"
                if not init_file.exists():
                    init_file.write_text('"""Zoaholic Plugins"""\n')
    
    def _get_plugin_module_name(self, file_path: Path) -> str:
        """生成插件模块名"""
        return f"zoaholic_plugins.{file_path.stem}"
    
    def _extract_plugin_info(self, module: Any, name: str, path: str, source: str) -> PluginInfo:
        """从模块中提取插件信息"""
        info = PluginInfo(
            name=name,
            path=path,
            source=source,
            module=module
        )
        
        # 尝试获取 PLUGIN_INFO 字典
        if hasattr(module, "PLUGIN_INFO"):
            plugin_meta = module.PLUGIN_INFO
            info.version = plugin_meta.get("version", "unknown")
            info.description = plugin_meta.get("description", "")
            info.author = plugin_meta.get("author", "")
            info.dependencies = plugin_meta.get("dependencies", [])
            info.metadata = plugin_meta.get("metadata", {})
            if "name" in plugin_meta:
                info.name = plugin_meta["name"]
        
        # 尝试获取扩展点信息
        if hasattr(module, "EXTENSIONS"):
            info.extensions = module.EXTENSIONS
        
        return info
    
    def load_from_file(self, file_path: str, overwrite: bool = False) -> Optional[PluginInfo]:
        """
        从文件加载单个插件
        
        Args:
            file_path: Python 文件路径
            overwrite: 是否覆盖已加载的同名插件
            
        Returns:
            PluginInfo 或 None（加载失败时）
        """
        path = Path(file_path)
        if not path.exists():
            logger.error(f"Plugin file not found: {file_path}")
            return None
        
        if not path.suffix == ".py":
            logger.error(f"Invalid plugin file (must be .py): {file_path}")
            return None
        
        if path.name.startswith("_"):
            logger.debug(f"Skipping private module: {file_path}")
            return None
        
        plugin_name = path.stem
        
        # 检查是否已加载
        if plugin_name in self._plugins:
            if not overwrite:
                logger.warning(f"Plugin already loaded: {plugin_name}")
                return self._plugins[plugin_name]
            else:
                logger.info(f"Overwriting existing plugin: {plugin_name}")
                self.unload_plugin(plugin_name)
                # 从插件列表中移除，以便重新加载
                if plugin_name in self._plugins:
                    del self._plugins[plugin_name]
        
        try:
            # 动态加载模块
            module_name = self._get_plugin_module_name(path)
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"Cannot load spec for {path}")
            
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            # 提取插件信息
            info = self._extract_plugin_info(module, plugin_name, str(path), "directory")
            
            self._plugins[plugin_name] = info
            logger.info(f"Loaded plugin: {plugin_name}")
            
            return info
            
        except Exception as e:
            error_msg = f"Failed to load plugin {plugin_name}: {e}"
            logger.error(error_msg)
            
            # 记录失败的插件
            info = PluginInfo(
                name=plugin_name,
                path=str(path),
                source="directory",
                enabled=False,
                error=str(e)
            )
            self._plugins[plugin_name] = info
            return info
    
    def load_from_directory(self, directory: Optional[str] = None) -> List[PluginInfo]:
        """
        从目录加载所有插件
        
        Args:
            directory: 插件目录路径，默认使用配置的插件目录
            
        Returns:
            已加载的插件信息列表
        """
        loaded_plugins: List[PluginInfo] = []
        
        # 示例插件文件名（仅作为示例，不参与实际加载）
        example_files = {
            "example_channel.py",
        }
        
        dirs_to_scan = [Path(directory)] if directory else self._plugin_dirs
        
        for plugin_dir in dirs_to_scan:
            if not plugin_dir.exists():
                logger.debug(f"Plugin directory not found: {plugin_dir}")
                continue
            
            logger.info(f"Scanning plugin directory: {plugin_dir}")
            
            # 扫描 .py 文件
            for file_path in plugin_dir.glob("*.py"):
                # 跳过以下情况：
                # 1) 以 "_" 开头的私有模块
                # 2) 官方示例插件文件（example_channel.py、example_hooks.py）
                if file_path.name.startswith("_"):
                    continue
                if file_path.name in example_files:
                    logger.debug(f"Skipping example plugin file: {file_path.name}")
                    continue
                
                info = self.load_from_file(str(file_path))
                if info:
                    loaded_plugins.append(info)
            
            # 扫描子目录（作为包）
            for subdir in plugin_dir.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("_"):
                    init_file = subdir / "__init__.py"
                    if init_file.exists():
                        info = self.load_from_file(str(init_file))
                        if info:
                            info.name = subdir.name
                            loaded_plugins.append(info)
        
        return loaded_plugins
    
    def load_from_entry_points(self) -> List[PluginInfo]:
        """
        从 Python entry points 加载插件
        
        插件包需要在 pyproject.toml 中声明：
        [project.entry-points."zoaholic.plugins"]
        my_plugin = "my_package.plugin"
        
        Returns:
            已加载的插件信息列表
        """
        loaded_plugins: List[PluginInfo] = []
        
        try:
            # Python 3.10+
            from importlib.metadata import entry_points
            eps = entry_points(group=self.ENTRY_POINT_GROUP)
        except TypeError:
            # Python 3.9
            from importlib.metadata import entry_points as _entry_points
            all_eps = _entry_points()
            eps = all_eps.get(self.ENTRY_POINT_GROUP, [])
        except ImportError:
            logger.debug("importlib.metadata not available")
            return loaded_plugins
        
        for ep in eps:
            plugin_name = ep.name
            
            if plugin_name in self._plugins:
                logger.debug(f"Plugin already loaded: {plugin_name}")
                continue
            
            try:
                # 加载模块
                module = ep.load()
                
                info = self._extract_plugin_info(module, plugin_name, str(ep.value), "entry_point")
                
                self._plugins[plugin_name] = info
                loaded_plugins.append(info)
                logger.info(f"Loaded plugin from entry point: {plugin_name}")
                
            except Exception as e:
                error_msg = f"Failed to load entry point {plugin_name}: {e}"
                logger.error(error_msg)
                
                info = PluginInfo(
                    name=plugin_name,
                    path=str(ep.value),
                    source="entry_point",
                    enabled=False,
                    error=str(e)
                )
                self._plugins[plugin_name] = info
                loaded_plugins.append(info)
        
        return loaded_plugins
    
    def load_from_module(self, module_path: str, plugin_name: Optional[str] = None, overwrite: bool = False) -> Optional[PluginInfo]:
        """
        动态加载 Python 模块作为插件
        
        Args:
            module_path: 模块路径，如 "my_package.my_plugin"
            plugin_name: 可选的插件名称
            overwrite: 是否覆盖已加载的同名插件
            
        Returns:
            PluginInfo 或 None
        """
        name = plugin_name or module_path.split(".")[-1]
        
        if name in self._plugins:
            if not overwrite:
                logger.warning(f"Plugin already loaded: {name}")
                return self._plugins[name]
            else:
                logger.info(f"Overwriting existing dynamic plugin: {name}")
                self.unload_plugin(name)
                if name in self._plugins:
                    del self._plugins[name]
        
        try:
            module = importlib.import_module(module_path)
            
            info = self._extract_plugin_info(module, name, module_path, "dynamic")
            
            self._plugins[name] = info
            logger.info(f"Loaded dynamic plugin: {name}")
            
            return info
            
        except Exception as e:
            error_msg = f"Failed to load module {module_path}: {e}"
            logger.error(error_msg)
            return None
    
    def unload_plugin(self, plugin_name: str) -> bool:
        """
        卸载插件
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            是否成功卸载
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin not found: {plugin_name}")
            return False
        
        info = self._plugins[plugin_name]
        
        try:
            # 尝试调用 unload 函数
            if info.module and hasattr(info.module, "unload"):
                info.module.unload()
                logger.info(f"Called unload() for plugin: {plugin_name}")
            
            # 从模块缓存中移除
            module_name = self._get_plugin_module_name(Path(info.path))
            if module_name in sys.modules:
                del sys.modules[module_name]
            
            # 标记为禁用
            info.enabled = False
            
            logger.info(f"Unloaded plugin: {plugin_name}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to unload plugin {plugin_name}: {e}")
            return False
    
    def reload_plugin(self, plugin_name: str) -> Optional[PluginInfo]:
        """
        重新加载插件（热重载）
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            重新加载后的 PluginInfo 或 None
        """
        if plugin_name not in self._plugins:
            logger.warning(f"Plugin not found: {plugin_name}")
            return None
        
        info = self._plugins[plugin_name]
        path = info.path
        source = info.source
        
        # 先卸载
        self.unload_plugin(plugin_name)
        
        # 从插件列表中移除
        del self._plugins[plugin_name]
        
        # 重新加载
        if source == "directory":
            return self.load_from_file(path)
        elif source == "dynamic":
            return self.load_from_module(path, plugin_name)
        else:
            logger.warning(f"Cannot reload entry point plugin: {plugin_name}")
            return None
    
    def load_all(self) -> Dict[str, List[PluginInfo]]:
        """
        加载所有可用的插件
        
        Returns:
            按来源分组的插件信息
        """
        self._ensure_plugin_dirs()
        
        result = {
            "directory": self.load_from_directory(),
            "entry_point": self.load_from_entry_points()
        }
        
        total = sum(len(plugins) for plugins in result.values())
        successful = sum(
            len([p for p in plugins if p.enabled]) 
            for plugins in result.values()
        )
        
        logger.info(f"Plugin loading complete: {successful}/{total} plugins loaded successfully")
        
        return result
    
    def get_plugin(self, plugin_name: str) -> Optional[PluginInfo]:
        """获取插件信息"""
        return self._plugins.get(plugin_name)
    
    def get_status(self) -> Dict[str, Any]:
        """
        获取加载器状态
        
        Returns:
            加载器状态信息
        """
        plugins_info = []
        for name, info in self._plugins.items():
            plugins_info.append({
                "name": info.name,
                "version": info.version,
                "description": info.description,
                "author": info.author,
                "source": info.source,
                "path": info.path,
                "enabled": info.enabled,
                "extensions": info.extensions,
                "loaded_at": info.loaded_at.isoformat(),
                "error": info.error
            })
        
        return {
            "plugin_dirs": [str(d) for d in self._plugin_dirs],
            "total_plugins": len(self._plugins),
            "enabled_plugins": len([p for p in self._plugins.values() if p.enabled]),
            "plugins": plugins_info
        }