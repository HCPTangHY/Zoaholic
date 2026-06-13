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
    client_ip = get_client_ip_from_request_info()
    if is_global_ip_blocked(app, client_ip):
        # 修改原因：全局 IP 黑名单必须优先于 API Key 身份检查执行。
        # 修改方式：在 token 存在性和 Key 匹配之前读取 middleware 写入的 client_ip 并检查全局缓存。
        # 目的：被全局禁止的 IP 无论使用哪个 Key 都直接返回 ip_blocked。
        raise_ip_blocked()

    if not token:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    # 修改原因：BYOK token 不是配置中的完整 api key，精确匹配失败后还需要按通配符前缀解析。
    # 修改方式：先保持原有精确匹配，再用 app.state.byok_prefixes 做最长前缀匹配；统计身份只使用模板 key。
    # 目的：让用户提交 byok-gemini-<真实上游key> 时通过本地鉴权，同时避免真实 key 进入 request_info。
    api_index: Optional[int] = None
    token_for_stats = token
    byok_real_key: Optional[str] = None
    byok_template_key: Optional[str] = None
    try:
        api_index = api_list.index(token)
        try:
            from core.byok import is_byok_api_key

            if is_byok_api_key(getattr(app.state, "api_keys_db", []), api_index):
                # 修改原因：BYOK 配置中的 byok-xxx-* 是本地模板身份，不是可直接使用的完整客户端 key。
                # 修改方式：精确命中通配符模板时不立即放行，继续走前缀解析；前缀解析会拒绝 real_key == "*"。
                # 目的：强制客户端必须提交 byok-xxx-<真实上游key>，不能只提交模板 key。
                api_index = None
        except Exception:
            pass
    except ValueError:
        api_index = None

    if api_index is None:
        try:
            from core.byok import get_byok_prefixes, resolve_byok_token

            byok_result = resolve_byok_token(token, get_byok_prefixes(app))
            if byok_result is not None:
                api_index, byok_template_key, byok_real_key = byok_result
                token_for_stats = byok_template_key
        except Exception:
            api_index = None

    # 兼容 admin JWT：映射到 admin api_key 的 index
    if api_index is None:
        try:
            from core.jwt_utils import is_admin_jwt

            if is_admin_jwt(token):
                api_index = _resolve_admin_api_index(app)
                if api_index is not None and 0 <= api_index < len(api_list):
                    token_for_stats = api_list[api_index]
        except Exception:
            api_index = None

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing API Key")

    if is_key_ip_blocked(app, api_index, client_ip):
        # 修改原因：通过身份解析后才能知道当前请求命中的 API Key，Key 级黑名单应在此时检查。
        # 修改方式：用 api_index 读取 app.state.api_key_ip_blacklists 的对应预解析规则。
        # 目的：实现“全局黑名单 → 当前 Key 黑名单”的固定检查顺序。
        raise_ip_blocked()

    try:
        from core.byok import store_byok_request_state, update_request_info_auth

        store_byok_request_state(
            request,
            byok_real_key=byok_real_key,
            template_key=byok_template_key,
            token_for_stats=token_for_stats,
        )
        # 修改原因：StatsMiddleware 会先用原始 Header 初始化 request_info，普通 Depends 鉴权成功后也需要同步真实配置身份。
        # 修改方式：复用 BYOK 模块中的统计字段更新函数，普通 key、admin JWT 和 BYOK 都统一写 api_key/name/group。
        # 目的：避免方言或普通路由的统计记录保留 dialect-pending、JWT 或原始 BYOK 真实 key。
        update_request_info_auth(app, api_index, token_for_stats, byok_real_key, byok_template_key)
    except Exception:
        pass

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
    api_list = app.state.api_list

    api_index: Optional[int] = None
    try:
        api_index = api_list.index(token)
        try:
            from core.byok import is_byok_api_key

            if is_byok_api_key(getattr(app.state, "api_keys_db", []), api_index):
                # 修改原因：BYOK 配置中的 byok-xxx-* 是本地模板身份，不是可直接使用的完整客户端 key。
                # 修改方式：精确命中通配符模板时不立即放行，继续走前缀解析；前缀解析会拒绝 real_key == "*"。
                # 目的：强制客户端必须提交 byok-xxx-<真实上游key>，不能只提交模板 key。
                api_index = None
        except Exception:
            pass
    except ValueError:
        api_index = None

    if api_index is None:
        raise HTTPException(status_code=403, detail="Invalid or missing credentials")

    if is_key_ip_blocked(app, api_index, client_ip):
        # 修改原因：admin API Key 也可以配置 Key 级 IP 黑名单。
        # 修改方式：普通 admin key 匹配成功后按 api_index 检查预解析黑名单。
        # 目的：管理端和用户端都遵守同一层级的访问控制规则。
        raise_ip_blocked()

    # 单 key 情况直接视为 admin
    if len(api_list) == 1:
        return token

    # 检查配置中的角色
    if "admin" not in app.state.api_keys_db[api_index].get("role", ""):
        raise HTTPException(status_code=403, detail="Permission denied")

    return token