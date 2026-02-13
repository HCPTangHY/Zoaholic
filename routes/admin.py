"""
Admin 管理路由
"""

import os
import string
import secrets

from fastapi import APIRouter, Depends, Body
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from core.env import env_bool
from utils import update_config
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

    if updated:
        # 配置持久化策略：
        # - CONFIG_STORAGE=file（默认）：api.yaml 为权威，前端保存必须写回文件，否则重启会被 api.yaml “改回去”
        # - CONFIG_STORAGE=auto/db：沿用 DB 持久化（云平台默认），可选 SYNC_CONFIG_TO_FILE 回写文件
        config_storage = (os.getenv("CONFIG_STORAGE") or "file").strip().lower()

        save_to_db = config_storage in ("auto", "db")
        # auto/file：始终写回 api.yaml，保证 yaml 权威；db：默认不写文件（可用 SYNC_CONFIG_TO_FILE 打开）
        save_to_file = (config_storage in ("file", "auto")) or env_bool("SYNC_CONFIG_TO_FILE", False)

        app.state.config, app.state.api_keys_db, app.state.api_list = await update_config(
            app.state.config,
            use_config_url=False,
            skip_model_fetch=True,
            save_to_file=save_to_file,
            save_to_db=save_to_db,
        )

    return JSONResponse(content={"message": "API config updated"})