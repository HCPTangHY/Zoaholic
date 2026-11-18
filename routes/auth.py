"""账号密码 + JWT 认证

用于管理控制台/管理接口。

注意：
- 路由使用 /auth 前缀（非 /v1），避免被 StatsMiddleware 拦截。
- API 网关的用户侧依旧支持 API Key。
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.jwt_utils import issue_jwt, decode_jwt
from core.initialization import ensure_initialized, get_admin_user
from core.auth import verify_admin_api_key
from core.security import verify_password
from routes.deps import rate_limit_dependency, get_app
from db import DISABLE_DATABASE


router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=6, max_length=256)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    role: str = "admin"


class MeResponse(BaseModel):
    username: str
    role: str


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(rate_limit_dependency)])
async def login(payload: LoginRequest = Body(...)):
    if DISABLE_DATABASE:
        raise HTTPException(status_code=500, detail="Database is disabled; cannot login.")

    await ensure_initialized(get_app())
    admin_user = await get_admin_user()

    if admin_user is None:
        raise HTTPException(status_code=404, detail="Admin user not initialized. Please visit /setup.")

    if admin_user.username != payload.username:
        raise HTTPException(status_code=403, detail="Invalid username or password")

    if not verify_password(payload.password, admin_user.password_hash):
        raise HTTPException(status_code=403, detail="Invalid username or password")

    token = issue_jwt({"sub": admin_user.username, "role": "admin"})
    return LoginResponse(access_token=token)


@router.get("/me", response_model=MeResponse, dependencies=[Depends(rate_limit_dependency)])
async def me(request: Request):
    token = await verify_admin_api_key(request, None)
    payload = decode_jwt(token)
    if not payload or str(payload.get("role", "")).lower() != "admin":
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    role = str(payload.get("role", "user"))
    username = str(payload.get("sub", ""))
    return MeResponse(username=username, role=role)
