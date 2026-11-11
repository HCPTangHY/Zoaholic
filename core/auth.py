"""
认证与限流模块

提供全局的 HTTPBearer、安全校验和速率限制依赖。
所有路由建议只从此模块导入 verify_api_key / verify_admin_api_key / rate_limit_dependency。
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from core.ip_blacklist import (
    get_client_ip_from_request_info,
    is_global_ip_blocked,
    is_key_ip_blocked,
    raise_ip_blocked,
)
from core.log_config import logger
from core.initialization import ensure_initialized, require_initialized
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


def is_api_key_disabled(app, api_index: Optional[int]) -> bool:
    """检查 api_keys 条目是否被管理员禁用。未设置 enabled 字段视为启用。"""
    if api_index is None:
        return False
    try:
        entry = app.state.api_keys_db[api_index]
    except Exception:
        return False
    return isinstance(entry, dict) and entry.get("enabled", True) is False


def _is_admin_jwt_token(token: Optional[str]) -> bool:
    """判断 token 是否是管理控制台 JWT（用于禁用检查的控制台豁免）。"""
    try:
        from core.jwt_utils import is_admin_jwt
        return bool(token) and is_admin_jwt(token)
    except Exception:
        return False


def check_key_ancestry_access(app, api_index: int, client_ip, *, admin_jwt: bool = False) -> None:
    """Check activation and IP policy without replacing the caller identity."""
    from core.key_hierarchy import key_chain

    keys = app.state.api_keys_db
    config = {"api_keys": keys}
    indices = {key['api']: index for index, key in enumerate(keys)}
    for key in key_chain(config, keys[api_index]['api']):
        index = indices[key['api']]
        if is_api_key_disabled(app, index) and not admin_jwt:
            raise HTTPException(status_code=403, detail="API Key has been disabled")
        if is_key_ip_blocked(app, index, client_ip):
            raise_ip_blocked()


def authenticate_api_key_token(
    app,
    token: Optional[str],
    client_ip: Optional[str],
    request: Optional[Request] = None,
) -> int:
    """统一校验普通/方言请求的 API Key（含全局与 Key 级 IP 黑名单、BYOK 前缀、Admin JWT、Key 禁用检查与统计身份回写）。"""
    require_initialized(app)
    if is_global_ip_blocked(app, client_ip):
        raise_ip_blocked()

    if not token:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    from core.byok import (
        get_byok_prefixes,
        is_byok_api_key,
        resolve_byok_token,
        store_byok_request_state,
        update_request_info_auth,
    )
    from core.jwt_utils import is_admin_jwt

    api_list = getattr(app.state, "api_list", [])
    api_index: Optional[int] = None
    token_for_stats = token
    byok_real_key: Optional[str] = None
    byok_template_key: Optional[str] = None

    try:
        api_index = api_list.index(token)
        if is_byok_api_key(getattr(app.state, "api_keys_db", []), api_index):
            # BYOK 模板 key 不能直接作为客户端 key 使用，必须走前缀解析。
            api_index = None
    except ValueError:
        api_index = None

    if api_index is None:
        byok_result = resolve_byok_token(token, get_byok_prefixes(app))
        if byok_result is not None:
            api_index, byok_template_key, byok_real_key = byok_result
            token_for_stats = byok_template_key

    if api_index is None and is_admin_jwt(token):
        api_index = _resolve_admin_api_index(app)
        if api_index is not None and 0 <= api_index < len(api_list):
            token_for_stats = api_list[api_index]

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    check_key_ancestry_access(app, api_index, client_ip, admin_jwt=_is_admin_jwt_token(token))

    if request is not None:
        store_byok_request_state(
            request,
            byok_real_key=byok_real_key,
            template_key=byok_template_key,
            token_for_stats=token_for_stats,
        )
        update_request_info_auth(app, api_index, token_for_stats, byok_real_key, byok_template_key)
        from core.middleware import request_info
        from core.key_hierarchy import snapshot_key_ancestry
        info = request_info.get()
        if info and getattr(request, 'scope', {}).get('type') != 'websocket':
            snapshot_key_ancestry(info, app.state.api_keys_db[api_index])

    return api_index


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
    await ensure_initialized(request.app)
    token = await _extract_token(request, credentials)
    client_ip = get_client_ip_from_request_info()
    return authenticate_api_key_token(request.app, token, client_ip, request=request)


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
    await ensure_initialized(app)

    token = await _extract_token(request, credentials)
    client_ip = get_client_ip_from_request_info()
    if is_global_ip_blocked(app, client_ip):
        # 修改原因：管理接口同样属于请求入口，全局 IP 黑名单应覆盖管理员凭证。
        # 修改方式：在 JWT 或 admin key 校验前检查全局黑名单。
        # 目的：被全局禁止的 IP 不能绕过管理接口修改配置。
        raise_ip_blocked()

    if not token:
        raise HTTPException(status_code=403, detail="Invalid or missing credentials")

    # 1) 先尝试当作 JWT
    try:
        from core.jwt_utils import is_admin_jwt

        if is_admin_jwt(token):
            admin_index = _resolve_admin_api_index(app)
            if admin_index is not None and is_key_ip_blocked(app, admin_index, client_ip):
                # 修改原因：admin JWT 会映射到配置中的 admin API Key，仍应遵守该 Key 的 IP 黑名单。
                # 修改方式：解析出 admin API Key 下标后检查对应 Key 级黑名单。
                # 目的：保证 JWT 管理入口与普通 API Key 管理入口遵循同一套 IP 访问控制。
                raise_ip_blocked()
            return token
    except HTTPException:
        # 修改原因：JWT 分支中可能抛出 IP 黑名单拦截错误，不能被宽泛异常处理吞掉。
        # 修改方式：先单独重新抛出 HTTPException，再处理 jwt 模块不可用等普通异常。
        # 目的：确保 admin JWT 命中 Key 级 IP 黑名单时直接返回 ip_blocked。
        raise
    except Exception:
        # jwt 模块不可用/异常则继续按 api key 处理
        pass

    # 2) 回退按 admin API key 处理
    return authenticate_admin_api_key_token(app, token, client_ip)


def authenticate_admin_api_key_token(app, token: Optional[str], client_ip: Optional[str], *, require_explicit_role=False) -> str:
    """Static admin credential only; setup recovery is the sole caller bypassing readiness."""
    if is_global_ip_blocked(app, client_ip):
        raise_ip_blocked()
    if not token:
        raise HTTPException(status_code=403, detail="Invalid or missing credentials")
    api_list = app.state.api_list

    api_index: Optional[int] = None
    try:
        from core.byok import is_byok_api_key

        api_index = api_list.index(token)
        if is_byok_api_key(getattr(app.state, "api_keys_db", []), api_index):
            # BYOK 配置中的 byok-xxx-* 是本地模板身份，不是可直接使用的完整客户端 key。
            api_index = None
    except ValueError:
        api_index = None

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing credentials")

    # 禁用的 admin Key 同样不能用于管理接口（JWT 路径已在上方提前返回，不受影响）
    check_key_ancestry_access(app, api_index, client_ip)

    if is_key_ip_blocked(app, api_index, client_ip):
        # 修改原因：admin API Key 也可以配置 Key 级 IP 黑名单。
        # 修改方式：普通 admin key 匹配成功后按 api_index 检查预解析黑名单。
        # 目的：管理端和用户端都遵守同一层级的访问控制规则。
        raise_ip_blocked()

    if require_explicit_role:
        if str(app.state.api_keys_db[api_index].get("role", "")).lower() != "admin":
            raise HTTPException(status_code=403, detail="An explicitly configured administrator API Key is required")
        return token

    # 单 key 情况直接视为 admin
    if len(api_list) == 1:
        return token

    # 检查配置中的角色
    if "admin" not in app.state.api_keys_db[api_index].get("role", ""):
        raise HTTPException(status_code=403, detail="Permission denied")

    return token