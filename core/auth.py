"""
认证与限流模块

提供全局的 HTTPBearer、安全校验和速率限制依赖。
所有路由建议只从此模块导入 verify_api_key / verify_admin_api_key / rate_limit_dependency。
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from core.log_config import logger
from utils import InMemoryRateLimiter

# 全局安全方案和速率限制器
security = HTTPBearer(auto_error=False)  # 设置 auto_error=False 以便我们自己处理缺失的情况
rate_limiter = InMemoryRateLimiter()


async def _extract_token(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = None) -> Optional[str]:
    """
    从请求中提取 API token，支持两种方式：
    1. x-api-key 头部
    2. Authorization: Bearer <token>
    """
    # 优先使用 x-api-key
    if request.headers.get("x-api-key"):
        return request.headers.get("x-api-key")
    
    # 其次使用 Authorization Bearer
    if credentials and credentials.credentials:
        return credentials.credentials
    
    # 最后尝试手动解析 Authorization 头
    auth_header = request.headers.get("Authorization")
    if auth_header:
        parts = auth_header.split(" ")
        if len(parts) > 1:
            return parts[1]
    
    return None


async def rate_limit_dependency(request: Request):
    """
    全局速率限制依赖

    根据 app.state.global_rate_limit 对所有请求进行限流。
    """
    app = request.app
    if await rate_limiter.is_rate_limited("global", app.state.global_rate_limit):
        raise HTTPException(status_code=429, detail="Too many requests")


def _resolve_admin_api_index(app) -> Optional[int]:
    """从当前 app.state.api_keys_db 中解析 admin key 的索引。

    说明：
    - 管理控制台使用 JWT 登录（/auth/login）。
    - 但网关的 /v1 端点鉴权/统计仍以“配置中的 API Key”作为计费/分组依据。
    - 因此当收到 admin JWT 时，需要把它映射到某个 admin API Key 的 api_index。
    """

    api_keys_db = getattr(app.state, "api_keys_db", None) or []
    if isinstance(api_keys_db, list):
        for i, item in enumerate(api_keys_db):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).lower()
            if "admin" in role:
                return i

        # 单 key 情况默认视为 admin
        if len(api_keys_db) == 1:
            return 0

    return None


async def verify_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> int:
    """
    验证普通 API Key 并返回其在配置中的索引

    支持：
    - x-api-key 头部
    - Authorization: Bearer <api_key>

    兼容：
    - 管理控制台的 admin JWT（Authorization: Bearer <jwt>）
      会被映射到配置中的 admin API Key 的 api_index。
    """
    app = request.app
    api_list = app.state.api_list

    token = await _extract_token(request, credentials)

    if not token:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    api_index: Optional[int] = None
    try:
        api_index = api_list.index(token)
    except ValueError:
        api_index = None

    # 兼容 admin JWT：映射到 admin api_key 的 index
    if api_index is None:
        try:
            from core.jwt_utils import is_admin_jwt

            if is_admin_jwt(token):
                api_index = _resolve_admin_api_index(app)
        except Exception:
            api_index = None

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    return api_index


async def verify_admin_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """验证管理员凭证。

    兼容两种方式：
    1) 传统：admin API Key（Authorization Bearer / x-api-key）
    2) 新方式：JWT（Authorization: Bearer <jwt>，payload.role=admin）

    返回：原始 token 字符串（可能是 api key 或 jwt）。
    """

    app = request.app

    token = await _extract_token(request, credentials)
    if not token:
        raise HTTPException(status_code=403, detail="Invalid or missing credentials")

    # 1) 先尝试当作 JWT
    try:
        from core.jwt_utils import is_admin_jwt

        if is_admin_jwt(token):
            return token
    except Exception:
        # jwt 模块不可用/异常则继续按 api key 处理
        pass

    # 2) 回退按 admin API key 处理
    api_list = app.state.api_list

    api_index: Optional[int] = None
    try:
        api_index = api_list.index(token)
    except ValueError:
        api_index = None

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing credentials")

    # 单 key 情况直接视为 admin
    if len(api_list) == 1:
        return token

    # 检查配置中的角色
    if "admin" not in app.state.api_keys_db[api_index].get("role", ""):
        raise HTTPException(status_code=403, detail="Permission denied")

    return token