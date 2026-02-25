"""
Admin 管理路由
"""

import os
import string
import secrets

from fastapi import APIRouter, Depends, Body, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from core.env import env_bool
from utils import update_config, API_YAML_PATH, yaml, dump_config_to_json_obj
from routes.deps import rate_limit_dependency, verify_admin_api_key, get_app

router = APIRouter()


@router.get("/v1/generate-api-key", dependencies=[Depends(rate_limit_dependency)])
async def generate_api_key():
    """
    生成新的 API Key
    """
    # 定义字符集（仅字母数字）
    chars = string.ascii_letters + string.digits
    # 生成 48 个字符的随机字符串
    random_string = ''.join(secrets.choice(chars) for _ in range(48))
    api_key = "sk-" + random_string
    return JSONResponse(content={"api_key": api_key})


@router.get("/v1/api_config", dependencies=[Depends(rate_limit_dependency)])
async def api_config(api_index: int = Depends(verify_admin_api_key)):
    """
    获取当前 API 配置
    """
    app = get_app()
    encoded_config = jsonable_encoder(app.state.config)
    return JSONResponse(content={"api_config": encoded_config})


@router.post("/v1/api_config/update", dependencies=[Depends(rate_limit_dependency)])
async def api_config_update(
    api_index: int = Depends(verify_admin_api_key),
    config: dict = Body(...)
):
    """
    更新 API 配置
    """
    app = get_app()
    updated = False

    # 支持同时更新 providers、api_keys 和 preferences 段，保持与 /v1/api_config 返回结构一致
    if "providers" in config:
        app.state.config["providers"] = config["providers"]
        updated = True

    if "api_keys" in config:
        app.state.config["api_keys"] = config["api_keys"]
        updated = True

    # 更新全局 preferences（包括 SCHEDULING_ALGORITHM 等设置）
    if "preferences" in config:
        if "preferences" not in app.state.config:
            app.state.config["preferences"] = {}
        app.state.config["preferences"].update(config["preferences"])
        updated = True

    if not updated:
        raise HTTPException(
            status_code=400,
            detail="No updatable sections provided. Allowed keys: providers, api_keys, preferences.",
        )

    # 配置持久化策略：
    # - CONFIG_STORAGE=file（默认）：api.yaml 为权威，前端保存必须写回文件，否则重启会回滚
    # - CONFIG_STORAGE=auto/db：可写 DB；其中 auto/file 默认也写回 api.yaml
    config_storage = (os.getenv("CONFIG_STORAGE") or "file").strip().lower()

    save_to_db = config_storage in ("auto", "db")
    # auto/file：始终写回 api.yaml，保证 yaml 权威；db：默认不写文件（可用 SYNC_CONFIG_TO_FILE 打开）
    save_to_file = (config_storage in ("file", "auto")) or env_bool("SYNC_CONFIG_TO_FILE", False)

    try:
        app.state.config, app.state.api_keys_db, app.state.api_list = await update_config(
            app.state.config,
            use_config_url=False,
            skip_model_fetch=True,
            save_to_file=save_to_file,
            save_to_db=save_to_db,
        )
    except Exception as e:
        # 不允许“假成功”：只要持久化过程有异常，直接返回非 200
        raise HTTPException(status_code=500, detail=f"Failed to update/persist config: {e}") from e

    # 进一步防止“假成功”：当本次要求写 yaml 时，回读文件校验关键段一致。
    if save_to_file:
        try:
            with open(API_YAML_PATH, "r", encoding="utf-8") as f:
                file_config = yaml.load(f) or {}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Config write verification failed (cannot read api.yaml): {e}") from e

        # 仅比较“可持久化配置”，忽略运行时字段（例如 providers 下的 _model_dict_cache）
        runtime_persistable = dump_config_to_json_obj(app.state.config or {})
        file_persistable = dump_config_to_json_obj(file_config or {})

        # 仅校验本次请求涉及到的 section，避免历史遗留差异阻塞无关保存
        sections_to_verify = [key for key in ("providers", "api_keys", "preferences") if key in config]

        runtime_subset = {
            "providers": runtime_persistable.get("providers", []),
            "api_keys": runtime_persistable.get("api_keys", []),
            "preferences": runtime_persistable.get("preferences", {}),
        }
        file_subset = {
            "providers": file_persistable.get("providers", []),
            "api_keys": file_persistable.get("api_keys", []),
            "preferences": file_persistable.get("preferences", {}),
        }

        runtime_encoded = runtime_subset
        file_encoded = file_subset

        mismatched_sections = [
            key for key in sections_to_verify
            if runtime_encoded.get(key) != file_encoded.get(key)
        ]
        if mismatched_sections:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Config write verification failed: api.yaml is out of sync. "
                    f"path={API_YAML_PATH}, verify_sections={sections_to_verify}, mismatched_sections={mismatched_sections}"
                ),
            )

    return JSONResponse(content={
        "message": "API config updated",
        "persisted": {
            "save_to_file": save_to_file,
            "save_to_db": save_to_db,
            "api_yaml_path": API_YAML_PATH if save_to_file else None,
        },
    })