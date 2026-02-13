"""Setup / 初始化向导

目标：像 newapi 一样，首次启动时可以通过 Web UI/接口设置管理员账号密码，并初始化系统配置。

注意：
- 该路由使用 /setup 前缀（非 /v1），避免被 StatsMiddleware 对 /v1 的 API Key 鉴权拦截。
"""

import os
import secrets
from typing import Optional

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from core.log_config import logger
from core.security import hash_password, verify_password
from routes.deps import get_app
from utils import update_config, load_config_from_db
from db import DISABLE_DATABASE, async_session_scope


router = APIRouter(prefix="/setup", tags=["Setup"])


def _get_config_storage() -> str:
    """配置存储策略。

    默认 file：以 api.yaml 为权威配置源。
    """

    return (os.getenv("CONFIG_STORAGE") or "file").strip().lower()


class SetupStatus(BaseModel):
    needs_setup: bool
    has_config: bool
    has_admin_user: bool


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


async def _ensure_admin_user_table():
    # 表结构由 core/stats.create_tables() 负责创建；这里不额外处理。
    return


async def _get_admin_user():
    # 延迟导入，避免循环引用
    from db import AdminUser

    if DISABLE_DATABASE:
        return None

    from db import DB_TYPE

    async with async_session_scope() as db:
        if (DB_TYPE or "sqlite").lower() == "d1":
            row = await db.query_one(
                "SELECT id, username, password_hash, jwt_secret FROM admin_user WHERE id = ?",
                [1],
            )
            if not row:
                return None
            return AdminUser(
                id=int(row.get("id") or 1),
                username=str(row.get("username") or ""),
                password_hash=str(row.get("password_hash") or ""),
                jwt_secret=row.get("jwt_secret"),
            )
        return await db.get(AdminUser, 1)


async def _upsert_admin_user(username: str, password: str, jwt_secret: str) -> None:
    from db import AdminUser

    if DISABLE_DATABASE:
        raise HTTPException(status_code=500, detail="Database is disabled; cannot persist admin user.")

    from db import DB_TYPE

    pwd_hash = hash_password(password)
    jwt_secret = (jwt_secret or "").strip()

    async with async_session_scope() as db:
        if (DB_TYPE or "sqlite").lower() == "d1":
            existing = await db.query_one("SELECT id, jwt_secret FROM admin_user WHERE id = ?", [1])
            if existing is None:
                await db.execute(
                    "INSERT INTO admin_user (id, username, password_hash, jwt_secret) VALUES (?, ?, ?, ?)",
                    [1, username, pwd_hash, jwt_secret],
                )
            else:
                existing_secret = existing.get("jwt_secret")
                next_secret = existing_secret if existing_secret else jwt_secret
                await db.execute(
                    "UPDATE admin_user SET username = ?, password_hash = ?, jwt_secret = ? WHERE id = ?",
                    [username, pwd_hash, next_secret, 1],
                )
        else:
            existing = await db.get(AdminUser, 1)
            if existing is None:
                existing = AdminUser(id=1, username=username, password_hash=pwd_hash, jwt_secret=jwt_secret)
                db.add(existing)
            else:
                existing.username = username
                existing.password_hash = pwd_hash
                # 若之前没有 jwt_secret，则补上
                if not getattr(existing, "jwt_secret", None):
                    existing.jwt_secret = jwt_secret
            await db.commit()


def _generate_admin_api_key() -> str:
    # 尽量保持 sk- 前缀风格
    return "sk-" + secrets.token_urlsafe(36)


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
    # 需要初始化：没有配置 或 没有管理员账号
    admin_user = await _get_admin_user()
    needs_setup = (not has_config) or (admin_user is None)

    return SetupStatus(
        needs_setup=needs_setup,
        has_config=has_config,
        has_admin_user=admin_user is not None,
    )


@router.post("/init", response_model=SetupInitResponse)
async def setup_init(payload: SetupInitRequest = Body(...)):
    """首次初始化：设置管理员账号密码，并写入最小可用配置到 DB。

    返回：管理员 API Key（用于 OpenAI 兼容 API 的管理接口鉴权）。

    注意：
    - 后续建议通过 /auth/login 使用“账号密码 + JWT”登录管理控制台。
    """

    if DISABLE_DATABASE:
        raise HTTPException(
            status_code=500,
            detail="Database is disabled; cannot run setup wizard. Please set DISABLE_DATABASE=false and provide DATABASE_URL.",
        )

    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    app = get_app()

    # 兼容“配置已存在但管理员账号丢失/未写入”的修复场景：
    # - 这种情况下 /auth/login 会 404（Admin user not initialized）
    # - 但 /setup/init 又会因为已有 api_list 而 409
    # 这里允许在 has_config=True 且 admin_user 为空时补建管理员账号，而不覆盖现有配置。
    admin_user = await _get_admin_user()
    has_config = bool(getattr(app.state, "api_list", None))

    # 如果配置和管理员账号都已存在，则拒绝重复初始化
    if has_config and admin_user is not None:
        raise HTTPException(status_code=409, detail="Already initialized")

    # 1) 写入/更新管理员账号 + 生成并持久化 JWT secret（用户无需手动配置 JWT_SECRET 环境变量）
    jwt_secret = secrets.token_urlsafe(48)
    await _upsert_admin_user(payload.username.strip(), payload.password, jwt_secret)

    # 同步到当前进程（避免无需重启即可登录）
    try:
        from core.jwt_utils import set_jwt_secret

        set_jwt_secret(jwt_secret)
    except Exception:
        pass

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

        # 更新内存标记
        app.state.needs_setup = False
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

    app.state.needs_setup = False
    app.state.admin_api_key = [admin_api_key]

    logger.info("Setup initialized successfully; admin key created.")
    return SetupInitResponse(admin_api_key=admin_api_key)


@router.post("/login", response_model=SetupLoginResponse)
async def setup_login(payload: SetupLoginRequest = Body(...)):
    """使用管理员账号密码登录，返回管理员 API Key（兼容现有前端基于 API Key 的鉴权）。"""

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
