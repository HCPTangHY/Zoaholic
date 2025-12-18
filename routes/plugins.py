"""
插件管理 API 路由

提供插件的列表、上传、启用/禁用、重载、卸载等管理功能。
"""

import shutil
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from routes.deps import rate_limit_dependency, verify_admin_api_key
from core.log_config import logger
from core.plugins import get_plugin_manager, get_interceptor_registry


router = APIRouter(prefix="/v1/plugins", tags=["plugins"])

# 插件目录
PLUGINS_DIR = Path("plugins")


def _get_plugin_info_dict(info) -> dict:
    """将 PluginInfo 转换为字典"""
    return {
        "name": info.name,
        "version": info.version,
        "description": info.description,
        "author": info.author,
        "source": info.source,
        "path": info.path,
        "enabled": info.enabled,
        "extensions": info.extensions,
        "dependencies": info.dependencies,
        "loaded_at": info.loaded_at.isoformat() if info.loaded_at else None,
        "error": info.error,
        "metadata": info.metadata,
    }


@router.get("", dependencies=[Depends(rate_limit_dependency)])
async def list_plugins(_: int = Depends(verify_admin_api_key)):
    """
    列出所有已加载的插件
    """
    manager = get_plugin_manager()
    plugins = manager.plugins
    
    plugins_list = [_get_plugin_info_dict(info) for info in plugins.values()]
    
    return JSONResponse(content={
        "plugins": plugins_list,
        "total": len(plugins_list),
    })


@router.get("/status", dependencies=[Depends(rate_limit_dependency)])
async def plugin_status(_: int = Depends(verify_admin_api_key)):
    """
    获取插件系统状态
    """
    manager = get_plugin_manager()
    return JSONResponse(content=manager.get_status())


@router.get("/extensions", dependencies=[Depends(rate_limit_dependency)])
async def list_extensions(_: int = Depends(verify_admin_api_key)):
    """
    列出所有已注册的扩展
    """
    manager = get_plugin_manager()
    registry = manager.registry
    
    extensions_by_point = {}
    for ep_name, extensions in registry.list_extensions().items():
        extensions_by_point[ep_name] = [
            {
                "id": ext.id,
                "extension_point": ext.extension_point,
                "priority": ext.priority,
                "enabled": ext.enabled,
                "plugin_name": ext.plugin_name,
                "metadata": ext.metadata,
            }
            for ext in extensions
        ]
    
    return JSONResponse(content={
        "extensions": extensions_by_point,
        "stats": registry.get_stats(),
    })


@router.get("/interceptors", dependencies=[Depends(rate_limit_dependency)])
async def list_interceptor_plugins(_: int = Depends(verify_admin_api_key)):
    """
    列出所有注册了拦截器的插件
    
    返回所有注册了请求/响应拦截器的插件列表，用于渠道配置时选择启用哪些插件。
    """
    interceptor_registry = get_interceptor_registry()
    manager = get_plugin_manager()
    
    # 获取所有注册了拦截器的插件
    interceptor_plugins = interceptor_registry.get_interceptor_plugins()
    
    # 补充插件的详细信息
    result = []
    for plugin_info in interceptor_plugins:
        plugin_name = plugin_info["plugin_name"]
        full_info = manager.loader.get_plugin(plugin_name)
        
        result.append({
            "plugin_name": plugin_name,
            "version": full_info.version if full_info else None,
            "description": full_info.description if full_info else None,
            "author": full_info.author if full_info else None,
            "enabled": full_info.enabled if full_info else False,
            "request_interceptors": plugin_info["request_interceptors"],
            "response_interceptors": plugin_info["response_interceptors"],
        })
    
    return JSONResponse(content={
        "interceptor_plugins": result,
        "total": len(result),
        "stats": interceptor_registry.get_stats(),
    })


@router.get("/extension-points", dependencies=[Depends(rate_limit_dependency)])
async def list_extension_points(_: int = Depends(verify_admin_api_key)):
    """
    列出所有扩展点
    """
    manager = get_plugin_manager()
    points = manager.list_extension_points()
    
    return JSONResponse(content={
        "extension_points": [
            {
                "name": ep.name,
                "type": ep.type.value if hasattr(ep.type, 'value') else str(ep.type),
                "description": ep.description,
                "required_methods": ep.required_methods,
                "optional_methods": ep.optional_methods,
                "singleton": ep.singleton,
                "priority_support": ep.priority_support,
            }
            for ep in points
        ]
    })


@router.get("/{name}", dependencies=[Depends(rate_limit_dependency)])
async def get_plugin(name: str, _: int = Depends(verify_admin_api_key)):
    """
    获取单个插件详情
    """
    manager = get_plugin_manager()
    info = manager.loader.get_plugin(name)
    
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    
    return JSONResponse(content={"plugin": _get_plugin_info_dict(info)})


@router.post("/upload", dependencies=[Depends(rate_limit_dependency)])
async def upload_plugin(
    file: UploadFile = File(...),
    _: int = Depends(verify_admin_api_key),
):
    """
    上传并安装插件
    
    支持 .py 单文件或 .zip 压缩包
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    
    filename = file.filename
    
    # 检查文件类型
    if not (filename.endswith(".py") or filename.endswith(".zip")):
        raise HTTPException(
            status_code=400, 
            detail="Invalid file type. Only .py or .zip files are allowed"
        )
    
    manager = get_plugin_manager()
    
    try:
        if filename.endswith(".py"):
            # 单文件插件
            plugin_name = filename[:-3]  # 移除 .py 后缀
            target_path = PLUGINS_DIR / filename
            
            # 保存文件（支持覆盖已存在的文件）
            content = await file.read()
            target_path.write_bytes(content)
            
            # 加载插件
            info = manager.load_plugin(str(target_path))
            
            if info and info.error:
                # 加载失败，删除文件
                target_path.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail=f"Failed to load plugin: {info.error}")
            
            logger.info(f"Uploaded and loaded plugin: {plugin_name}")
            
            return JSONResponse(content={
                "success": True,
                "plugin": _get_plugin_info_dict(info) if info else None,
                "message": f"Plugin '{plugin_name}' uploaded and loaded successfully",
            })
        
        else:
            # ZIP 压缩包
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir) / filename
                
                # 保存临时文件
                content = await file.read()
                temp_path.write_bytes(content)
                
                # 解压
                with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                    # 安全检查：防止路径穿越
                    for member in zip_ref.namelist():
                        if member.startswith('/') or '..' in member:
                            raise HTTPException(
                                status_code=400, 
                                detail=f"Invalid path in zip: {member}"
                            )
                    
                    zip_ref.extractall(temp_dir)
                
                # 查找插件目录或文件
                extracted_items = list(Path(temp_dir).iterdir())
                extracted_items = [p for p in extracted_items if p.name != filename]
                
                if not extracted_items:
                    raise HTTPException(status_code=400, detail="Empty zip file")
                
                # 如果只有一个目录，使用该目录
                if len(extracted_items) == 1 and extracted_items[0].is_dir():
                    plugin_dir = extracted_items[0]
                    plugin_name = plugin_dir.name
                    target_path = PLUGINS_DIR / plugin_name
                    
                    # 如果目标目录已存在，先删除旧目录以支持覆盖
                    if target_path.exists():
                        shutil.rmtree(target_path, ignore_errors=True)
                    
                    # 复制目录
                    shutil.copytree(plugin_dir, target_path)
                    
                    # 加载插件
                    init_file = target_path / "__init__.py"
                    if init_file.exists():
                        info = manager.load_plugin(str(init_file))
                    else:
                        # 尝试加载目录中的 .py 文件
                        py_files = list(target_path.glob("*.py"))
                        if py_files:
                            info = manager.load_plugin(str(py_files[0]))
                        else:
                            shutil.rmtree(target_path, ignore_errors=True)
                            raise HTTPException(
                                status_code=400, 
                                detail="No Python files found in plugin directory"
                            )
                else:
                    # 多个文件，创建以 zip 文件名命名的目录
                    plugin_name = filename[:-4]  # 移除 .zip 后缀
                    target_path = PLUGINS_DIR / plugin_name
                    
                    # 如果目标目录已存在，先删除旧目录以支持覆盖
                    if target_path.exists():
                        shutil.rmtree(target_path, ignore_errors=True)
                    
                    target_path.mkdir(parents=True, exist_ok=True)
                    
                    for item in extracted_items:
                        if item.is_file():
                            shutil.copy2(item, target_path / item.name)
                        else:
                            shutil.copytree(item, target_path / item.name)
                    
                    # 加载插件
                    init_file = target_path / "__init__.py"
                    if init_file.exists():
                        info = manager.load_plugin(str(init_file))
                    else:
                        py_files = list(target_path.glob("*.py"))
                        if py_files:
                            info = manager.load_plugin(str(py_files[0]))
                        else:
                            shutil.rmtree(target_path, ignore_errors=True)
                            raise HTTPException(
                                status_code=400, 
                                detail="No Python files found in extracted content"
                            )
                
                if info and info.error:
                    # 加载失败，清理
                    if target_path.is_dir():
                        shutil.rmtree(target_path, ignore_errors=True)
                    else:
                        target_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=400, detail=f"Failed to load plugin: {info.error}")
                
                logger.info(f"Uploaded and loaded plugin from zip: {plugin_name}")
                
                return JSONResponse(content={
                    "success": True,
                    "plugin": _get_plugin_info_dict(info) if info else None,
                    "message": f"Plugin '{plugin_name}' uploaded and loaded successfully",
                })
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to upload plugin: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload plugin: {str(e)}")


@router.post("/{name}/enable", dependencies=[Depends(rate_limit_dependency)])
async def enable_plugin(name: str, _: int = Depends(verify_admin_api_key)):
    """
    启用插件
    """
    manager = get_plugin_manager()
    info = manager.loader.get_plugin(name)
    
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    
    if info.enabled:
        return JSONResponse(content={
            "success": True,
            "message": f"Plugin '{name}' is already enabled",
        })
    
    # 重新加载以启用
    new_info = manager.reload_plugin(name)
    
    if new_info and new_info.enabled:
        return JSONResponse(content={
            "success": True,
            "plugin": _get_plugin_info_dict(new_info),
            "message": f"Plugin '{name}' enabled successfully",
        })
    else:
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to enable plugin '{name}'"
        )


@router.post("/{name}/disable", dependencies=[Depends(rate_limit_dependency)])
async def disable_plugin(name: str, _: int = Depends(verify_admin_api_key)):
    """
    禁用插件
    """
    manager = get_plugin_manager()
    info = manager.loader.get_plugin(name)
    
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    
    if not info.enabled:
        return JSONResponse(content={
            "success": True,
            "message": f"Plugin '{name}' is already disabled",
        })
    
    # 卸载插件（但保留文件）
    success = manager.unload_plugin(name)
    
    # 同时注销该插件的拦截器
    interceptor_registry = get_interceptor_registry()
    interceptor_registry.unregister_plugin_interceptors(name)
    
    if success:
        return JSONResponse(content={
            "success": True,
            "message": f"Plugin '{name}' disabled successfully",
        })
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to disable plugin '{name}'"
        )


@router.post("/{name}/reload", dependencies=[Depends(rate_limit_dependency)])
async def reload_plugin(name: str, _: int = Depends(verify_admin_api_key)):
    """
    重载插件（热更新）
    """
    manager = get_plugin_manager()
    info = manager.loader.get_plugin(name)
    
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    
    # 先注销拦截器
    interceptor_registry = get_interceptor_registry()
    interceptor_registry.unregister_plugin_interceptors(name)
    
    # 重载插件
    new_info = manager.reload_plugin(name)
    
    if new_info:
        return JSONResponse(content={
            "success": True,
            "plugin": _get_plugin_info_dict(new_info),
            "message": f"Plugin '{name}' reloaded successfully",
        })
    else:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reload plugin '{name}'"
        )


@router.delete("/{name}", dependencies=[Depends(rate_limit_dependency)])
async def uninstall_plugin(name: str, _: int = Depends(verify_admin_api_key)):
    """
    卸载并删除插件
    """
    manager = get_plugin_manager()
    info = manager.loader.get_plugin(name)
    
    if not info:
        raise HTTPException(status_code=404, detail=f"Plugin '{name}' not found")
    
    # 不允许删除内置插件（entry_point 来源）
    if info.source == "entry_point":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot uninstall entry_point plugin '{name}'. Use pip uninstall instead."
        )
    
    # 注销拦截器
    interceptor_registry = get_interceptor_registry()
    interceptor_registry.unregister_plugin_interceptors(name)
    
    # 卸载插件
    manager.unload_plugin(name)
    
    # 删除文件
    plugin_path = Path(info.path)
    try:
        if plugin_path.is_file():
            plugin_path.unlink()
        elif plugin_path.parent.name == name:
            # 删除整个插件目录
            shutil.rmtree(plugin_path.parent, ignore_errors=True)
        
        # 从插件列表中移除
        if name in manager.loader._plugins:
            del manager.loader._plugins[name]
        
        logger.info(f"Uninstalled plugin: {name}")
        
        return JSONResponse(content={
            "success": True,
            "message": f"Plugin '{name}' uninstalled successfully",
        })
    
    except Exception as e:
        logger.error(f"Failed to delete plugin files: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Plugin unloaded but failed to delete files: {str(e)}"
        )


@router.post("/load-all", dependencies=[Depends(rate_limit_dependency)])
async def load_all_plugins(_: int = Depends(verify_admin_api_key)):
    """
    加载所有插件（用于初始化或刷新）
    """
    manager = get_plugin_manager()
    result = manager.load_all()
    
    total = sum(len(plugins) for plugins in result.values())
    successful = sum(
        len([p for p in plugins if p.enabled]) 
        for plugins in result.values()
    )
    
    return JSONResponse(content={
        "success": True,
        "message": f"Loaded {successful}/{total} plugins successfully",
        "result": {
            source: [_get_plugin_info_dict(p) for p in plugins]
            for source, plugins in result.items()
        },
    })