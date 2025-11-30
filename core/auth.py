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


async def verify_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> int:
    """
    验证普通 API Key 并返回其在配置中的索引
    支持 x-api-key 头部和 Authorization: Bearer 格式
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

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")
    
    return api_index


async def verify_admin_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> str:
    """
    验证管理员 API Key，返回 token 字符串
    支持 x-api-key 头部和 Authorization: Bearer 格式
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

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    # 单 key 情况直接视为 admin
    if len(api_list) == 1:
        return token

    # 检查配置中的角色
    if "admin" not in app.state.api_keys_db[api_index].get("role", ""):
        raise HTTPException(status_code=403, detail="Permission denied")

    return token