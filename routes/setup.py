"""Setup / 初始化向导

目标：像 newapi 一样，首次启动时可以通过 Web UI/接口设置管理员账号密码，并初始化系统配置。

注意：
- 该路由使用 /setup 前缀（非 /v1），避免被 StatsMiddleware 对 /v1 的 API Key 鉴权拦截。
"""

import asyncio
import os
import secrets
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from core.log_config import logger
from core.auth import _extract_token, authenticate_admin_api_key_token
from core.initialization import ensure_initialized, get_admin_user, refresh_initialization
from core.security import hash_password, verify_password
from routes.deps import get_app
from utils import update_config, load_config_from_db
from db import DISABLE_DATABASE, async_session_scope


router = APIRouter(prefix="/setup", tags=["Setup"])
_setup_lock = asyncio.Lock()


def _get_config_storage() -> str:
    """配置存储策略。

    默认 file：以 api.yaml 为权威配置源。
    """

    return (os.getenv("CONFIG_STORAGE") or "file").strip().lower()


class SetupStatus(BaseModel):
    needs_setup: bool
    has_config: bool
    has_admin_user: bool
    database_enabled: bool


class SetupInitRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=256)
    confirm_password: str = Field(..., min_length=6, max_length=256)

    # 可选：允许用户自己指定管理员 API Key（否则自动生成）
    admin_api_key: Optional[str] = Field(default=None)


class SetupInitResponse(BaseModel):
    admin_api_key: str


class SetupLoginRequest(BaseModel):
    username: str
    password: str


class SetupLoginResponse(BaseModel):
    admin_api_key: str


async def _get_admin_user():
    return await get_admin_user()


async def _create_admin_user(username: str, password: str, jwt_secret: str) -> None:
    from db import AdminUser

    if DISABLE_DATABASE:
        raise HTTPException(status_code=500, detail="Database is disabled; cannot persist admin user.")

    from db import DB_TYPE

    pwd_hash = hash_password(password)
    jwt_secret = (jwt_secret or "").strip()

    # Keep the PR's create-only behavior. The unique id also arbitrates setup
    # requests from different workers; report the losing insert as a conflict.
    try:
        async with async_session_scope() as db:
            if (DB_TYPE or "sqlite").lower() == "d1":
                await db.execute(
                    "INSERT INTO admin_user (id, username, password_hash, jwt_secret) VALUES (?, ?, ?, ?)",
                    [1, username, pwd_hash, jwt_secret],
                )
            else:
                db.add(AdminUser(id=1, username=username, password_hash=pwd_hash, jwt_secret=jwt_secret))
                await db.commit()
    except Exception:
        if await _get_admin_user() is not None:
            raise HTTPException(status_code=409, detail="Administrator was initialized by another request") from None
        raise


def _generate_admin_api_key() -> str:
    # 使用 zk- 前缀
    return "zk-" + secrets.token_urlsafe(36)


def _select_admin_api_key_from_config(conf: dict) -> Optional[str]:
    api_keys = conf.get("api_keys") or []
    if not isinstance(api_keys, list):
        return None

    for item in api_keys:
        if not isinstance(item, dict):
            continue
        if "admin" in str(item.get("role", "")):
            key = item.get("api")
            if key:
                return str(key)

    # fallback：第一把 key
    if api_keys and isinstance(api_keys[0], dict) and api_keys[0].get("api"):
        return str(api_keys[0]["api"])

    return None


@router.get("/status", response_model=SetupStatus)
async def setup_status():
    app = get_app()

    has_config = bool(getattr(app.state, "api_list", None))
    try:
        admin_user = await _get_admin_user()
        ready = await refresh_initialization(app, force=True)
    except Exception:
        app.state.admin_initialized = False
        app.state.needs_setup = True
        raise HTTPException(status_code=503, detail="Initialization state is unavailable") from None
    needs_setup = not ready or not has_config

    return SetupStatus(
        needs_setup=needs_setup,
        has_config=has_config,
        has_admin_user=admin_user is not None,
        database_enabled=not DISABLE_DATABASE,
    )


@router.post("/init", response_model=SetupInitResponse)
async def setup_init(request: Request, payload: SetupInitRequest = Body(...)):
    """首次初始化：设置管理员账号密码，并写入最小可用配置到 DB。

    返回：管理员 API Key（用于 OpenAI 兼容 API 的管理接口鉴权）。

    注意：
    - 后续建议通过 /auth/login 使用“账号密码 + JWT”登录管理控制台。
    """

    async with _setup_lock:
        return await _setup_init(request, payload)


async def _setup_init(request: Request, payload: SetupInitRequest):
    if DISABLE_DATABASE:
        raise HTTPException(
            status_code=500,
            detail="Database is disabled; cannot run setup wizard. Please set DISABLE_DATABASE=false and provide DATABASE_URL.",
        )

    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    app = get_app()

    # 仅空白部署允许匿名初始化；已有配置的账号修复必须验证现有管理员凭证。
    admin_user = await _get_admin_user()
    has_config = bool(getattr(app.state, "api_list", None))

    # Existing accounts are never overwritten. A missing config can only be
    # recovered after proving ownership of that account, not by anonymous setup.
    if has_config and admin_user is not None:
        raise HTTPException(status_code=409, detail="Already initialized")
    if has_config:
        token = await _extract_token(request, None)
        from core.middleware import StatsMiddleware
        client_ip = StatsMiddleware._get_client_ip(request.scope, request.scope.get("headers", []))
        authenticate_admin_api_key_token(app, token, client_ip, require_explicit_role=True)
    elif admin_user is not None:
        if admin_user.username != payload.username.strip() or not verify_password(payload.password, admin_user.password_hash):
            raise HTTPException(status_code=403, detail="Existing administrator credentials are required")

    if admin_user is None:
        await _create_admin_user(payload.username.strip(), payload.password, secrets.token_urlsafe(48))

    # 2) 配置处理
    if has_config:
        # 配置已经存在：不覆盖，仅返回现有 admin_api_key；若配置里没有 admin key，才补一个进去。
        config_storage = _get_config_storage()
        # 配置权威：file/auto 优先内存态（来自 api.yaml），db 模式才优先 DB
        if config_storage == "db":
            conf_existing = await load_config_from_db() or getattr(app.state, "config", None) or {}
        else:
            conf_existing = getattr(app.state, "config", None) or {}
            # auto 模式下 DB 仅作为备份兜底
            if config_storage == "auto" and (not conf_existing):
                conf_existing = await load_config_from_db() or {}
        admin_api_key = _select_admin_api_key_from_config(conf_existing)

        if not admin_api_key:
            # 极端修复：配置里没有任何 key，则补一个
            admin_api_key = (payload.admin_api_key or "").strip() or _generate_admin_api_key()
            conf_existing = conf_existing if isinstance(conf_existing, dict) else {}
            conf_existing.setdefault("providers", [])
            conf_existing.setdefault("preferences", {})
            conf_existing.setdefault("api_keys", [])
            conf_existing["api_keys"].insert(
                0,
                {
                    "api": admin_api_key,
                    "role": "admin",
                    "model": ["all"],
                },
            )

            # 持久化并刷新内存态
            # - file：写回 api.yaml（保持 yaml 权威）
            # - auto/db：写回数据库（兼容云平台）
            save_to_db = config_storage in ("auto", "db")
            save_to_file = config_storage in ("file", "auto")
            app.state.config, app.state.api_keys_db, app.state.api_list = await update_config(
                conf_existing,
                use_config_url=False,
                skip_model_fetch=True,
                save_to_file=save_to_file,
                save_to_db=save_to_db,
            )
            # 修改原因：setup 修复配置后也需要刷新运行时 BYOK 前缀表。
            # 修改方式：基于最新 api_keys_db 单独重建 app.state.byok_prefixes。
            # 目的：首次配置或修复后无需重启即可使用 BYOK 通配符身份。
            from core.byok import build_byok_prefixes
            from core.ip_blacklist import apply_runtime_ip_blacklists
            app.state.byok_prefixes = build_byok_prefixes(app.state.api_keys_db)
            # 修改原因：setup 修复配置也会调用 update_config，可能同时写入或清空 IP 黑名单。
            # 修改方式：修复后重建运行时黑名单缓存。
            # 目的：首次修复或补 key 后运行时状态与配置文件保持一致。
            apply_runtime_ip_blacklists(app)

        # Only this authenticated recovery path (or startup) may repair a missing
        # persisted signing secret; the public status endpoint remains read-only.
        if not await refresh_initialization(app, force=True, repair_secret=True):
            raise HTTPException(status_code=503, detail="Initialization is incomplete; check database and JWT configuration")
        app.state.admin_api_key = [admin_api_key]

        logger.info("Setup repaired successfully; admin user created/updated.")
        return SetupInitResponse(admin_api_key=admin_api_key)

    # 没有配置：走首次初始化（生成最小配置）
    admin_api_key = (payload.admin_api_key or "").strip() or _generate_admin_api_key()

    conf_seed = {
        "providers": [],
        "api_keys": [
            {
                "api": admin_api_key,
                "role": "admin",
                "model": ["all"],
            }
        ],
        "preferences": {},
    }

    # 写入配置并更新内存态（file 模式写回 api.yaml；auto/db 模式写回 DB）
    config_storage = _get_config_storage()
    save_to_db = config_storage in ("auto", "db")
    save_to_file = config_storage in ("file", "auto")
    app.state.config, app.state.api_keys_db, app.state.api_list = await update_config(
        conf_seed,
        use_config_url=False,
        skip_model_fetch=True,
        save_to_file=save_to_file,
        save_to_db=save_to_db,
    )
    # 修改原因：首次初始化配置后也需要刷新运行时 BYOK 前缀表。
    # 修改方式：基于最新 api_keys_db 单独重建 app.state.byok_prefixes。
    # 目的：保持 setup 初始化路径和常规配置加载路径一致。
    from core.byok import build_byok_prefixes
    from core.ip_blacklist import apply_runtime_ip_blacklists
    app.state.byok_prefixes = build_byok_prefixes(app.state.api_keys_db)
    # 修改原因：首次初始化后也需要建立空 IP 黑名单缓存，避免鉴权读取缺失属性。
    # 修改方式：基于最新最小配置重建运行时黑名单。
    # 目的：让 setup 初始化路径与普通启动和管理端热更新路径一致。
    apply_runtime_ip_blacklists(app)

    if not await refresh_initialization(app, force=True, repair_secret=True):
        raise HTTPException(status_code=503, detail="Initialization is incomplete; check database and JWT configuration")
    app.state.admin_api_key = [admin_api_key]

    logger.info("Setup initialized successfully; admin key created.")
    return SetupInitResponse(admin_api_key=admin_api_key)


@router.post("/login", response_model=SetupLoginResponse)
async def setup_login(payload: SetupLoginRequest = Body(...)):
    """使用管理员账号密码登录，返回管理员 API Key（兼容现有前端基于 API Key 的鉴权）。"""

    await ensure_initialized(get_app())
    admin_user = await _get_admin_user()
    if admin_user is None:
        raise HTTPException(status_code=404, detail="Admin user not initialized")

    if admin_user.username != payload.username:
        raise HTTPException(status_code=403, detail="Invalid username or password")

    if not verify_password(payload.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="Invalid username or password")

    # 从当前配置中取管理员 key（配置权威来自 api.yaml/app.state.config）
    app = get_app()
    conf = getattr(app.state, "config", None) or {}
    key = _select_admin_api_key_from_config(conf)
    if not key:
        raise HTTPException(
            status_code=500,
            detail="No admin API key found in configuration. Please re-run setup.",
        )

    return SetupLoginResponse(admin_api_key=key)
