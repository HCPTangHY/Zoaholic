"""账号密码 + JWT 认证

用于管理控制台/管理接口。

注意：
- 路由使用 /auth 前缀（非 /v1），避免被 StatsMiddleware 拦截。
- API 网关的用户侧依旧支持 API Key。
"""

import os
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.jwt_utils import issue_jwt, decode_jwt
from core.security import verify_password
from routes.deps import rate_limit_dependency, get_app
from db import DISABLE_DATABASE, async_session_scope, AdminUser, DB_TYPE


router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    role: str = "admin"
    # 方便前端显示/生成外部客户端链接（可选）
    admin_api_key: Optional[str] = None


class MeResponse(BaseModel):
    username: str
    role: str
    admin_api_key: Optional[str] = None


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

    if api_keys and isinstance(api_keys[0], dict) and api_keys[0].get("api"):
        return str(api_keys[0]["api"])

    return None


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(rate_limit_dependency)])
async def login(payload: LoginRequest = Body(...)):
    if DISABLE_DATABASE:
        raise HTTPException(status_code=500, detail="Database is disabled; cannot login.")

    async with async_session_scope() as db:
        if (DB_TYPE or "sqlite").lower() == "d1":
            row = await db.query_one(
                "SELECT id, username, password_hash, jwt_secret FROM admin_user WHERE id = ?",
                [1],
            )
            admin_user = (
                AdminUser(
                    id=int(row.get("id") or 1),
                    username=str(row.get("username") or ""),
                    password_hash=str(row.get("password_hash") or ""),
                    jwt_secret=row.get("jwt_secret"),
                ) if row else None
            )
        else:
            admin_user = await db.get(AdminUser, 1)

    # 若没有显式配置 JWT_SECRET，则优先使用 DB 中持久化的 jwt_secret
    try:
        from core.jwt_utils import set_jwt_secret

        if not (os.getenv("JWT_SECRET") or "").strip():
            if admin_user is not None and getattr(admin_user, "jwt_secret", None):
                set_jwt_secret(str(admin_user.jwt_secret))
    except Exception:
        pass

    if admin_user is None:
        raise HTTPException(status_code=404, detail="Admin user not initialized. Please visit /setup.")

    if admin_user.username != payload.username:
        raise HTTPException(status_code=403, detail="Invalid username or password")

    if not verify_password(payload.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="Invalid username or password")

    token = issue_jwt({"sub": admin_user.username, "role": "admin"})

    # admin_api_key 仅用于前端展示/兼容旧鉴权；配置权威来自 api.yaml（app.state.config）
    app = get_app()
    conf = getattr(app.state, "config", None) or {}
    admin_api_key = _select_admin_api_key_from_config(conf)

    # 同步内存标记：已初始化
    app.state.needs_setup = False

    return LoginResponse(access_token=token, admin_api_key=admin_api_key)


@router.get("/me", response_model=MeResponse, dependencies=[Depends(rate_limit_dependency)])
async def me(request: Request):
    auth = request.headers.get("Authorization") or ""
    token = auth.split(" ", 1)[1].strip() if auth.startswith("Bearer ") else ""

    payload = decode_jwt(token)
    if not payload:
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    role = str(payload.get("role", "user"))
    username = str(payload.get("sub", ""))

    app = get_app()
    conf = getattr(app.state, "config", None) or {}
    admin_api_key = _select_admin_api_key_from_config(conf)

    return MeResponse(username=username, role=role, admin_api_key=admin_api_key)
