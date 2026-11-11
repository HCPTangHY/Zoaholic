"""
插件管理 API 路由

提供插件的列表、上传、启用/禁用、重载、卸载等管理功能。
"""

import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse

from routes.deps import rate_limit_dependency, verify_admin_api_key
from core.log_config import logger
from core.plugins import get_plugin_manager, get_interceptor_registry


router = APIRouter(prefix="/v1/plugins", tags=["plugins"])

# 插件目录
PLUGINS_DIR = Path("plugins")
MAX_PLUGIN_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_PLUGIN_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_PLUGIN_ZIP_ENTRIES = 1024


def _validate_plugin_name(name: str) -> None:
    if (not name or name != name.strip() or name in (".", "..")
            or name.startswith(".") or any(char in name for char in "/\\:")
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise HTTPException(400, "Plugin filename must be a plain filename without a path")


def _plugin_upload_root() -> Path:
    root = PLUGINS_DIR.absolute()
    # Do not follow an existing symlink in the configured installation path.
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise HTTPException(400, "Plugin installation directory cannot contain symlinks")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _plugin_upload_target(root: Path, name: str, *, directory: bool) -> Path:
    _validate_plugin_name(name)
    target = root / name
    if target.is_symlink() or not target.resolve().is_relative_to(root):
        raise HTTPException(400, "Plugin target is outside the installation directory")
    if target.exists() and target.is_dir() != directory:
        raise HTTPException(400, "Plugin target has a conflicting file type")
    return target


def _extract_plugin_zip(archive_path: Path, destination: Path) -> None:
    """Validate every member before extracting; never extract links or special files."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if not members:
            raise HTTPException(400, "Empty zip file")
        if len(members) > MAX_PLUGIN_ZIP_ENTRIES:
            raise HTTPException(413, "Plugin archive contains too many entries")
        total = 0
        seen = set()
        for member in members:
            name = member.filename
            if member.orig_filename != name:
                raise HTTPException(400, "Invalid plugin archive filename")
            parts = name.rstrip("/").split("/")
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (not name or name.startswith("/") or "\\" in name or ":" in name
                    or any(part in ("", ".", "..") for part in parts)
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or (mode == stat.S_IFDIR and not member.is_dir())
                    or (mode == stat.S_IFREG and member.is_dir())):
                raise HTTPException(400, "Invalid path or file type in plugin archive")
            relative = PurePosixPath(name)
            if relative in seen or member.flag_bits & 1:
                raise HTTPException(400, "Duplicate or encrypted plugin archive entry")
            seen.add(relative)
            total += member.file_size
            if total > MAX_PLUGIN_EXPANDED_BYTES:
                raise HTTPException(413, "Expanded plugin archive exceeds 64 MiB")

        destination.mkdir()
        extracted = 0
        for member in members:
            target = destination / member.filename
            if not target.resolve().is_relative_to(destination.resolve()):
                raise HTTPException(400, "Plugin archive path escapes extraction directory")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("xb") as output:
                while chunk := source.read(64 * 1024):
                    extracted += len(chunk)
                    if extracted > MAX_PLUGIN_EXPANDED_BYTES:
                        raise HTTPException(413, "Expanded plugin archive exceeds 64 MiB")
                    output.write(chunk)


def _remove_upload_target(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _install_uploaded_plugin(candidate: Path, target: Path, entry: Path, backup: Path):
    """Replace only the validated target; restore previous files if loading fails.

    This is not a sandbox: administrators intentionally execute trusted Python.
    Restoring files cannot undo module code that already ran before a load error.
    """
    manager = get_plugin_manager()
    previous = target.exists()
    if previous:
        target.replace(backup)
    try:
        candidate.replace(target)
        info = manager.load_plugin(str(target / entry) if target.is_dir() else str(target))
        if info is None:
            raise HTTPException(400, "Plugin loader did not accept the uploaded module")
        if info.error:
            raise HTTPException(400, f"Failed to load plugin: {info.error}")
        return info
    except BaseException:
        _remove_upload_target(target)
        if previous:
            backup.replace(target)
        raise


def _get_plugin_info_dict(info) -> dict:
    """将 PluginInfo 转换为字典，包含拦截器阶段信息"""
    result = {
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
    # 从拦截器 registry 补充各阶段信息
    try:
        registry = get_interceptor_registry()
        for plugin_info in registry.get_interceptor_plugins():
            if plugin_info.get("plugin_name") == info.name:
                for stage in ("inbound_interceptors", "channel_inbound_interceptors",
                              "request_interceptors", "response_interceptors",
                              "channel_outbound_interceptors", "key_outbound_interceptors"):
                    entries = plugin_info.get(stage, [])
                    if entries:
                        result[stage] = entries
                        # 合并 entry metadata 到顶层（description、params_hint 等）
                        for entry in entries:
                            for k, v in entry.get("metadata", {}).items():
                                if k != "stage" and k not in result.get("metadata", {}):
                                    if not isinstance(result["metadata"], dict):
                                        result["metadata"] = {}
                                    result["metadata"][k] = v
                break
    except Exception:
        pass
    return result


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
            # 修改原因：拦截器系统新增 channel_inbound、channel_outbound 和 key_outbound 三个阶段，插件列表接口不能只返回旧 request/response。
            # 修改方式：所有阶段字段都从 registry 输出中读取，不存在时回退为空数组，兼容旧运行时状态。
            # 目的：让前端可以通过阶段数组和条目 metadata.stage 识别插件适用位置。
            "inbound_interceptors": plugin_info.get("inbound_interceptors", []),
            "channel_inbound_interceptors": plugin_info.get("channel_inbound_interceptors", []),
            "request_interceptors": plugin_info.get("request_interceptors", []),
            "response_interceptors": plugin_info.get("response_interceptors", []),
            "channel_outbound_interceptors": plugin_info.get("channel_outbound_interceptors", []),
            "key_outbound_interceptors": plugin_info.get("key_outbound_interceptors", []),
            "balance_enrichers": plugin_info.get("balance_enrichers", []),
            "metadata": full_info.metadata if full_info else {},
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
    """Install an administrator-supplied Python plugin, bounded to the plugin directory."""
    filename = file.filename or ""
    _validate_plugin_name(filename)
    if not filename.endswith((".py", ".zip")):
        raise HTTPException(400, "Invalid file type. Only .py or .zip files are allowed")
    _validate_plugin_name(Path(filename).stem)

    try:
        root = _plugin_upload_root()
        # Same filesystem permits atomic rename; underscore keeps staging out of plugin scans.
        with tempfile.TemporaryDirectory(prefix="_upload_", dir=root) as temp_dir:
            staging = Path(temp_dir)
            uploaded = staging / "upload"
            size = 0
            with uploaded.open("xb") as output:
                while chunk := await file.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_PLUGIN_UPLOAD_BYTES:
                        raise HTTPException(413, "Plugin upload exceeds 16 MiB")
                    output.write(chunk)

            if filename.endswith(".py"):
                target = _plugin_upload_target(root, filename, directory=False)
                candidate, entry = uploaded, Path(filename)
                plugin_name = Path(filename).stem
            else:
                extracted = staging / "extracted"
                _extract_plugin_zip(uploaded, extracted)
                items = list(extracted.iterdir())
                candidate = items[0] if len(items) == 1 and items[0].is_dir() else extracted
                plugin_name = candidate.name if candidate != extracted else Path(filename).stem
                target = _plugin_upload_target(root, plugin_name, directory=True)
                if (candidate / "__init__.py").is_file():
                    entry = Path("__init__.py")
                else:
                    py_files = sorted(candidate.glob("*.py"))
                    if not py_files:
                        raise HTTPException(400, "No Python files found in plugin directory")
                    entry = Path(py_files[0].name)

            info = _install_uploaded_plugin(candidate, target, entry, staging / "previous")
            logger.info("Uploaded and loaded plugin: %s", plugin_name)
            return JSONResponse(content={
                "success": True,
                "plugin": _get_plugin_info_dict(info) if info else None,
                "message": f"Plugin '{plugin_name}' uploaded and loaded successfully",
            })
    except HTTPException:
        raise
    except (zipfile.BadZipFile, FileExistsError, NotADirectoryError, IsADirectoryError) as exc:
        raise HTTPException(400, "Invalid plugin archive or installation target") from exc
    except Exception as exc:
        logger.exception("Failed to upload plugin")
        raise HTTPException(500, "Failed to upload plugin") from exc


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


@router.post("/reload_all", dependencies=[Depends(rate_limit_dependency)])
async def reload_all_plugins(_: int = Depends(verify_admin_api_key)):
    """
    强制重新加载所有插件（先全部注销再重新扫描加载）
    """
    manager = get_plugin_manager()
    interceptor_registry = get_interceptor_registry()

    # 1. 注销所有已注册的插件拦截器
    existing_plugins = list(manager.loader.plugins.keys())
    for name in existing_plugins:
        try:
            interceptor_registry.unregister_plugin_interceptors(name)
        except Exception:
            pass

    # 2. 卸载所有插件
    for name in existing_plugins:
        try:
            manager.loader.unload_plugin(name)
        except Exception:
            pass

    # 3. 清空插件注册表
    manager.loader._plugins.clear()

    # 4. 重新扫描并加载
    result = manager.loader.load_all()

    # 5. 激活所有已启用的插件
    loaded = []
    failed = []
    for source, plugins in result.items():
        for info in plugins:
            if info.enabled:
                try:
                    manager._activate_plugin(info)
                    loaded.append(info.name)
                except Exception as e:
                    failed.append({"name": info.name, "error": str(e)})
                    logger.error(f"Failed to activate plugin {info.name}: {e}")
            else:
                loaded.append(info.name)

    logger.info(f"[plugins] reload_all: {len(loaded)} loaded, {len(failed)} failed")

    return JSONResponse(content={
        "success": True,
        "loaded": loaded,
        "failed": failed,
        "message": f"Reloaded {len(loaded)} plugins, {len(failed)} failed",
    })


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