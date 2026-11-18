"""账号密码 + JWT 认证

用于管理控制台/管理接口。

注意：
- 路由使用 /auth 前缀（非 /v1），避免被 StatsMiddleware 拦截。
- API 网关的用户侧依旧支持 API Key。
"""

from collections import deque
from time import monotonic
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.jwt_utils import issue_jwt, decode_jwt
from core.initialization import ensure_initialized, get_admin_user
from core.auth import select_admin_api_key, verify_admin_api_key
from core.ip_blacklist import extract_client_ip
from core.security import verify_password
from routes.deps import rate_limit_dependency, get_app
from db import DISABLE_DATABASE


router = APIRouter(prefix="/auth", tags=["Auth"])

MAX_LOGIN_FAILURES = 6
LOGIN_LOCKOUT_WINDOW_SECONDS = 600.0
MAX_LOGIN_FAILURE_IPS = 4096
_LOGIN_FAILURES: dict[str, deque[float]] = {}
_select_admin_api_key_from_config = select_admin_api_key


def _extract_login_client_ip(request: Optional[Request]) -> str:
    if request is None:
        return "unknown"
    client = getattr(request, "client", None)
    return extract_client_ip(getattr(request, "headers", None), getattr(client, "host", None) or "unknown")


def _check_login_lockout(client_ip: str) -> None:
    now = monotonic()
    bucket = _LOGIN_FAILURES.get(client_ip)
    if not bucket:
        return
    cutoff = now - LOGIN_LOCKOUT_WINDOW_SECONDS
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if not bucket:
        _LOGIN_FAILURES.pop(client_ip, None)
        return
    if len(bucket) >= MAX_LOGIN_FAILURES:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please try again later.",
        )


def _record_login_failure(client_ip: str) -> None:
    now = monotonic()
    cutoff = now - LOGIN_LOCKOUT_WINDOW_SECONDS
    bucket = _LOGIN_FAILURES.setdefault(client_ip, deque())
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    bucket.append(now)
    if len(_LOGIN_FAILURES) > MAX_LOGIN_FAILURE_IPS:
        expired_ips = [ip for ip, q in _LOGIN_FAILURES.items() if not q or q[-1] < cutoff]
        for ip in expired_ips:
            _LOGIN_FAILURES.pop(ip, None)
        while len(_LOGIN_FAILURES) > MAX_LOGIN_FAILURE_IPS:
            oldest_ip = next(iter(_LOGIN_FAILURES))
            _LOGIN_FAILURES.pop(oldest_ip, None)


def _clear_login_failures(client_ip: str) -> None:
    _LOGIN_FAILURES.pop(client_ip, None)


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
async def login(request: Request = None, payload: LoginRequest = Body(...)):
    if DISABLE_DATABASE:
        raise HTTPException(status_code=500, detail="Database is disabled; cannot login.")

    client_ip = _extract_login_client_ip(request)
    _check_login_lockout(client_ip)

    await ensure_initialized(get_app())
    admin_user = await get_admin_user()

    if admin_user is None:
        raise HTTPException(status_code=404, detail="Admin user not initialized. Please visit /setup.")

    if admin_user.username != payload.username:
        _record_login_failure(client_ip)
        raise HTTPException(status_code=403, detail="Invalid username or password")

    if not verify_password(payload.password, admin_user.password_hash):
        _record_login_failure(client_ip)
        raise HTTPException(status_code=403, detail="Invalid username or password")

    _clear_login_failures(client_ip)
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
