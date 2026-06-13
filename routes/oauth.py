"""OAuth 管理路由。"""

import base64
import html
import json
import secrets
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from routes.deps import verify_admin_api_key

router = APIRouter(tags=["OAuth"])

# 修改原因：manual 模式的 provider 可能需要覆盖 provider 类上的默认 localhost 回调地址。
# 修改方式：保留 provider type 到固定 redirect_uri 的覆盖表；未配置时使用 provider.localhost_redirect_uri。
# 目的：让特殊 provider 能在不修改 provider 类的情况下调整手动粘贴回调地址。
OAUTH_REDIRECT_URIS: dict[str, str] = {}

# 修改原因：浏览器 OAuth 登录需要在 authorize 和 callback/exchange 之间短期保存 state、PKCE verifier 和目标渠道。
# 修改方式：MVP 使用进程内 dict 保存 pending flow，并在每次发起授权时按 TTL 清理。
# 目的：不引入 Redis 也能完成最小可用的 CSRF 防护、授权码交换上下文保存和分渠道注册。
OAUTH_FLOW_TTL_SECONDS = 300
_pending_flows: dict[str, dict] = {}


def _encode_oauth_state(provider: str) -> str:
    """把 provider name 编码进 OAuth state。"""
    # 修改原因：OAuth callback 只能收到 code 和 state，不能依赖额外 query 参数携带渠道名。
    # 修改方式：把随机 nonce 与 provider name 序列化为紧凑 JSON，再用 URL 安全 base64 编码并去掉 padding。
    # 目的：让 state 本身包含目标渠道信息，同时仍保留 pending flow 中的 verifier 做 CSRF 和 PKCE 校验。
    payload = {"nonce": secrets.token_urlsafe(32), "provider": provider}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_oauth_state_provider(state: str) -> str | None:
    """从 OAuth state 中解出 provider name；旧 state 或损坏 state 返回 None。"""
    # 修改原因：升级前的 pending flow 测试和运行中旧授权 state 可能仍是纯随机字符串。
    # 修改方式：按 base64url JSON 尝试解码，任何异常都返回 None，由 pending flow 兼容路径继续处理。
    # 目的：新流程可从 state 校验渠道名，旧流程不会因为格式变化立即失效。
    try:
        padding = "=" * (-len(state) % 4)
        raw = base64.urlsafe_b64decode((state + padding).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    provider = str(payload.get("provider") or "").strip()
    return provider or None


def _cleanup_expired_flows(now: float | None = None) -> None:
    """清理超过 TTL 的 OAuth pending flow。"""
    # 修改原因：state 和 code_verifier 只应短期有效，长时间保存在内存中会增加误用和重放风险。
    # 修改方式：遍历内存 pending flow，删除 created_at 距当前时间超过 5 分钟的条目。
    # 目的：让 MVP 的内存存储具备基本生命周期控制，后续可平滑替换为 Redis。
    current = time.time() if now is None else now
    expired = [
        key
        for key, value in _pending_flows.items()
        if current - float(value.get("created_at", 0)) > OAUTH_FLOW_TTL_SECONDS
    ]
    for key in expired:
        _pending_flows.pop(key, None)


def _first_header_value(value: str | None) -> str:
    """取代理转发头中的第一个有效值。"""
    # 修改原因：X-Forwarded-Proto 和 X-Forwarded-Host 可能由多层代理追加为逗号分隔列表。
    # 修改方式：统一取第一个非空片段，并去掉首尾空白。
    # 目的：生成稳定的 Zoaholic 直连 OAuth callback 地址。
    if not value:
        return ""
    return value.split(",", 1)[0].strip()


def _build_redirect_uri(request: Request) -> str:
    """根据当前请求构建 Zoaholic 直连 OAuth callback 地址。"""
    # 修改原因：auto 模式需要把 provider 回调指向 Zoaholic 后端，且线上通常经过反向代理。
    # 修改方式：优先读取 X-Forwarded-Proto 和 X-Forwarded-Host，缺失时回退到 request.url 与 Host 头。
    # 目的：让 Google、Antigravity 等允许自定义 redirect_uri 的 provider 可以直连完成 token 交换。
    scheme = _first_header_value(request.headers.get("x-forwarded-proto")) or request.url.scheme
    host = (
        _first_header_value(request.headers.get("x-forwarded-host"))
        or _first_header_value(request.headers.get("host"))
        or request.url.netloc
    )
    return f"{scheme}://{host}/v1/oauth/callback"


def _oauth_error_page(title: str, message: str, status_code: int = 400) -> HTMLResponse:
    """生成 OAuth 失败提示页。"""
    # 修改原因：callback 是浏览器直接访问的页面，JSON 错误不利于用户理解授权结果。
    # 修改方式：把错误标题和内容 HTML 转义后渲染为简单页面。
    # 目的：在不泄露 HTML 注入风险的前提下，把失败原因展示给正在登录的用户。
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return HTMLResponse(f"<h2>{safe_title}</h2><p>{safe_message}</p>", status_code=status_code)


def _oauth_success_page(key_id: str, state: str, provider: str, already_exists: bool = False) -> HTMLResponse:
    """生成 OAuth 成功提示页，并通知前端窗口刷新账号列表。"""
    # 修改原因：callback 页面需要把新增账号标识和渠道名传回管理前端，but key_id/provider 不能直接拼进脚本字符串。
    # 修改方式：HTML 展示部分使用 html.escape，postMessage 载荷先用 json.dumps 序列化，再转义脚本敏感字符。
    # 目的：完成弹窗登录闭环，同时避免账号字符串中的特殊字符破坏 HTML、JavaScript 或 script 标签边界。
    safe_key_id = html.escape(key_id)
    message_payload = (
        json.dumps(
            {"type": "oauth_callback_success", "key_id": key_id, "state": state, "provider": provider, "already_exists": already_exists},
            ensure_ascii=False,
        )
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return HTMLResponse(f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>登录成功</title>
<style>
  body {{ font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #0a0a0a; color: #e5e5e5; }}
  .card {{ text-align: center; padding: 2rem; border-radius: 1rem; background: #1a1a1a; border: 1px solid #333; max-width: 400px; }}
  h2 {{ color: #22c55e; margin-bottom: 0.5rem; }}
  p {{ color: #999; font-size: 0.9rem; }}
  .email {{ color: #60a5fa; font-family: monospace; }}
</style>
</head>
<body>
<div class="card">
  <h2>登录成功</h2>
  <p>账号 <span class="email">{safe_key_id}</span> 已添加到 Zoaholic</p>
  <p>此窗口将在 3 秒后自动关闭</p>
</div>
<script>
  if (window.opener) {{
    window.opener.postMessage({message_payload}, '*');
  }}
  setTimeout(() => window.close(), 3000);
</script>
</body>
</html>
""")


def _token_data_from_body(body: dict) -> dict:
    """从导入请求中剥离路由控制字段，仅保留凭据字段。"""
    # 修改原因：key_id、type、provider 和 service_account_json 是路由控制字段，不应原样写入 token_data 后再被 register 二次覆盖。
    # 修改方式：复制 body 中除 key_id、type、provider、service_account_json 之外的字段。
    # 目的：让手动导入同时支持 refresh_token、access_token、id_token 等凭据字段，同时不污染凭据内容。
    return {k: v for k, v in body.items() if k not in {"key_id", "type", "provider", "service_account_json"}}


def _first_non_empty_string(*values) -> str:
    """返回第一个非空字符串值。"""
    # 修改原因：CPA 和 sub2api 导出文件中账号标识可能分布在 name、email、user.email、account.email_address 等不同字段。
    # 修改方式：统一把候选值转成字符串并去掉首尾空白，返回第一个非空结果。
    # 目的：让批量导入的 key_id 选择规则保持一致，避免每个格式分支重复手写兜底逻辑。
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _utc_iso_from_timestamp(value) -> str | None:
    """把 unix timestamp 秒转换为 ISO 8601 UTC 字符串。"""
    # 修改原因：sub2api 的 expires_at 使用 unix timestamp 秒，而 CPA 与管理端更常见的是 RFC3339/ISO 字符串。
    # 修改方式：接受数字或数字字符串，按 UTC 转为带 Z 后缀的 ISO 8601 字符串；无法解析时返回 None。
    # 目的：批量导入 sub2api 数据时保存稳定、可读的过期时间字段。
    if value is None or value == "":
        return None
    try:
        timestamp = float(value)
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse_batch_expiry_epoch(value) -> float | None:
    """把批量导入凭据中的过期时间解析为 epoch 秒。"""
    # 修改原因：批量导入的 expires_at 可能是数字、数字字符串或 RFC3339 字符串，路由需要判断无 refresh_token 的 token 是否已过期。
    # 修改方式：先尝试数字解析，再尝试 ISO/RFC3339 解析，并统一转换成 UTC epoch 秒。
    # 目的：避免把明显过期且无法刷新的 access_token 注册进 OAuthManager。
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    try:
        normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _batch_token_is_expired(token_data: dict) -> bool:
    """判断批量导入的凭据是否已经过期。"""
    # 修改原因：refresh 不可用时注册过期 access_token 会让后续请求立即失败，且错误不如导入时反馈清楚。
    # 修改方式：读取 expires_at，缺失时兼容 Gemini 的 expiry 字段，解析成功后与当前时间比较。
    # 目的：把无可刷新能力的过期凭据标记为 skipped，而不是写入不可用账号。
    expiry_value = token_data.get("expires_at")
    if expiry_value is None:
        expiry_value = token_data.get("expiry")
    expiry_epoch = _parse_batch_expiry_epoch(expiry_value)
    return expiry_epoch is not None and expiry_epoch <= time.time()


def _unknown_batch_import_item(item=None) -> dict:
    """生成未知格式的 normalize 结果。"""
    # 修改原因：批量导入需要逐条返回结果，遇到无法识别或非对象元素时也不能让整批解析失败。
    # 修改方式：保留 dict 中已有凭据字段；非 dict 使用空 token_data，并把账号标识兜底为 unknown。
    # 目的：让调用方可以在 results 中看到跳过原因，同时保留兼容未知导出结构的余地。
    token_data = dict(item) if isinstance(item, dict) else {}
    key_id = _first_non_empty_string(
        token_data.get("key_id"),
        token_data.get("email"),
        token_data.get("name"),
    ) or "unknown"
    return {"key_id": key_id, "token_data": token_data, "source_format": "unknown"}


def _normalize_sub2api_account(account) -> dict:
    """把 sub2api 单个账号转换为统一导入结构。"""
    # 修改原因：sub2api 把 OAuth 凭据放在 accounts[].credentials 中，账号标识放在 name 或 credentials.email 中。
    # 修改方式：复制 credentials 作为 token_data，并把 unix 秒 expires_at 转成 ISO 8601 字符串。
    # 目的：让 sub2api 导出可以直接批量导入，而不要求管理员手工拆分每个账号。
    if not isinstance(account, dict):
        return {"key_id": "unknown", "token_data": {}, "source_format": "sub2api"}
    credentials = account.get("credentials")
    token_data = dict(credentials) if isinstance(credentials, dict) else {}
    expires_at = _utc_iso_from_timestamp(token_data.get("expires_at"))
    if expires_at:
        token_data["expires_at"] = expires_at
    key_id = _first_non_empty_string(
        account.get("name"),
        token_data.get("email"),
        token_data.get("chatgpt_account_id"),
        token_data.get("account_id"),
    ) or "unknown"
    return {"key_id": key_id, "token_data": token_data, "source_format": "sub2api"}


def _normalize_cpa_import_item(item) -> dict:
    """把 CPA 单文件凭据转换为统一导入结构。"""
    # 修改原因：CPA 的 Codex、Claude 和 Gemini 导出字段不同，但后续注册逻辑只需要统一的 key_id 与 token_data。
    # 修改方式：按格式特征识别子类型，保留原始字段，同时补齐 email、account_id、organization_id、expires_at 等本地常用字段。
    # 目的：让 CPA 单文件和多文件合并数据复用同一条批量导入处理路径。
    if not isinstance(item, dict):
        return _unknown_batch_import_item(item)

    token_data = dict(item)

    if "expiry" in item:
        key_id = _first_non_empty_string(item.get("email")) or "unknown"
        if item.get("expiry"):
            token_data["expires_at"] = item.get("expiry")
        return {"key_id": key_id, "token_data": token_data, "source_format": "cpa_gemini"}

    account = item.get("account") if isinstance(item.get("account"), dict) else {}
    organization = item.get("organization") if isinstance(item.get("organization"), dict) else {}
    if account or organization or "expires_in" in item:
        key_id = _first_non_empty_string(
            account.get("email_address"),
            item.get("email"),
            organization.get("uuid"),
            account.get("uuid"),
        ) or "unknown"
        email = _first_non_empty_string(account.get("email_address"), item.get("email"))
        if email:
            token_data["email"] = email
        account_uuid = _first_non_empty_string(account.get("uuid"))
        if account_uuid:
            token_data["account_id"] = account_uuid
        org_uuid = _first_non_empty_string(organization.get("uuid"))
        if org_uuid:
            token_data["organization_id"] = org_uuid
        org_name = _first_non_empty_string(organization.get("name"))
        if org_name:
            token_data["organization_name"] = org_name
        try:
            expires_in = float(item.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in > 0:
            expires_at = time.time() + expires_in
            token_data["expires_at"] = expires_at
            token_data["expired"] = _utc_iso_from_timestamp(expires_at)
        return {"key_id": key_id, "token_data": token_data, "source_format": "cpa_claude"}

    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    if user or "expires_at" in item:
        key_id = _first_non_empty_string(user.get("email"), item.get("email"), user.get("id")) or "unknown"
        email = _first_non_empty_string(user.get("email"), item.get("email"))
        if email:
            token_data["email"] = email
        account_id = _first_non_empty_string(user.get("id"), item.get("account_id"))
        if account_id:
            token_data["account_id"] = account_id
        return {"key_id": key_id, "token_data": token_data, "source_format": "cpa_codex"}

    if item.get("token_type") and item.get("email"):
        key_id = _first_non_empty_string(item.get("email")) or "unknown"
        return {"key_id": key_id, "token_data": token_data, "source_format": "cpa_gemini"}

    return _unknown_batch_import_item(item)


def _normalize_batch_import_data(data) -> list[dict]:
    """把 sub2api 或 CPA 导出数据统一转换为批量导入账号列表。"""
    # 修改原因：/v1/oauth/batch_import 需要自动识别 sub2api、CPA 单文件和 CPA 多文件合并三种输入格式。
    # 修改方式：sub2api 识别 accounts 数组，CPA 多文件识别顶层数组，CPA 单文件识别顶层 access_token；其他结构保留为 unknown。
    # 目的：让后续批量注册逻辑只处理统一的 key_id、token_data、source_format 三元结构。
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return [_normalize_sub2api_account(account) for account in data.get("accounts", [])]
    if isinstance(data, list):
        return [_normalize_cpa_import_item(item) for item in data]
    if isinstance(data, dict) and "access_token" in data:
        return [_normalize_cpa_import_item(data)]
    if isinstance(data, dict):
        return [_unknown_batch_import_item(data)]
    return [{"key_id": "unknown", "token_data": {}, "source_format": "unknown"}]


def _key_exists_in_provider(app, channel_id: str, key_id: str) -> bool:
    """检查 key_id 是否已存在于指定渠道的 api 列表中。

    用于 OAuth 导入/登录时判断是否需要追加新行。
    支持 api 列表中的纯字符串、带 ! 前缀和 dict(带 label) 三种格式。
    """
    providers = (getattr(getattr(app, 'state', None), 'config', None) or {}).get('providers', [])
    for p in providers:
        if not isinstance(p, dict):
            continue
        if p.get('provider') != channel_id:
            continue
        api_list = p.get('api', [])
        if isinstance(api_list, str):
            api_list = [api_list]
        if not isinstance(api_list, list):
            break
        for item in api_list:
            if isinstance(item, dict) and len(item) == 1:
                raw_key = str(next(iter(item.keys()))).strip()
                clean = raw_key[1:] if raw_key.startswith('!') else raw_key
            elif isinstance(item, str):
                clean = item.strip()[1:] if item.strip().startswith('!') else item.strip()
            else:
                continue
            if clean == key_id:
                return True
        break
    return False


def _replace_api_key_id_entry(item, old_key_id: str, new_key_id: str):
    """替换 api 列表中的一个 key_id 条目。"""
    # 修改原因：OAuth key 可能以普通字符串、!禁用字符串或带备注 dict 三种格式保存在 api.yaml。
    # 修改方式：统一解析出干净 key_id，命中旧 key 时只替换 key 部分，并保留禁用前缀和备注值。
    # 目的：账号改名同步 api.yaml 时不破坏用户设置的禁用状态和 Key 备注。
    if isinstance(item, dict) and len(item) == 1:
        raw_key, label = next(iter(item.items()))
        raw_key = str(raw_key).strip()
        disabled = raw_key.startswith("!")
        clean_key = raw_key[1:] if disabled else raw_key
        if clean_key != old_key_id:
            return item, False
        replaced_key = f"!{new_key_id}" if disabled else new_key_id
        return {replaced_key: label}, True

    if isinstance(item, str):
        raw_key = item.strip()
        disabled = raw_key.startswith("!")
        clean_key = raw_key[1:] if disabled else raw_key
        if clean_key != old_key_id:
            return item, False
        return f"!{new_key_id}" if disabled else new_key_id, True

    return item, False


async def _sync_provider_api_key_rename(app, channel_id: str, old_key_id: str, new_key_id: str) -> bool:
    """把 OAuth 账号改名同步到当前 provider 的 api 配置并持久化。"""
    # 修改原因：rename API 原先只更新 OAuthManager state，api.yaml 中 provider.api 仍保留旧 key_id。
    # 修改方式：在 app.state.config.providers 中定位当前 provider，替换 api 字段里的旧 key，并复用 admin 的持久化流程写回配置。
    # 目的：用户在 OAuth Key 输入框失焦改名后，不需要再手动保存渠道也能让 api.yaml 与 oauth_state.json 保持一致。
    config = getattr(getattr(app, "state", None), "config", None)
    if not isinstance(config, dict):
        return False
    providers = config.get("providers")
    if not isinstance(providers, list):
        return False

    changed = False
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_name = str(provider.get("provider") or provider.get("name") or "").strip()
        if provider_name != channel_id:
            continue

        api_value = provider.get("api")
        if isinstance(api_value, list):
            replaced_items = []
            for item in api_value:
                replaced, item_changed = _replace_api_key_id_entry(item, old_key_id, new_key_id)
                replaced_items.append(replaced)
                changed = changed or item_changed
            if changed:
                provider["api"] = replaced_items
        elif isinstance(api_value, str) or (isinstance(api_value, dict) and len(api_value) == 1):
            replaced, changed = _replace_api_key_id_entry(api_value, old_key_id, new_key_id)
            if changed:
                provider["api"] = replaced
        break

    if not changed:
        return False

    from routes.admin import _persist_config

    await _persist_config(app, sections_to_verify=["providers"], changed_providers={channel_id})
    return True


def _require_provider_name(value: str | None) -> str | None:
    """规范化并校验 provider name。"""
    # 修改原因：所有 OAuth 凭据操作都必须限定渠道名，空 provider 会退回旧的全局账号语义。
    # 修改方式：把传入值转字符串并 trim，空值返回 None 供路由转成 400。
    # 目的：防止导入、登录、查询、删除等操作写入或读取错误渠道。
    provider_name = str(value or "").strip()
    return provider_name or None


@router.get("/v1/oauth/authorize", dependencies=[Depends(verify_admin_api_key)])
async def oauth_authorize(type: str, provider: str, request: Request, mode: str | None = None, origin: str | None = None):
    """发起 OAuth 授权，按 provider 能力返回直连回调或手动粘贴模式。"""
    # 修改原因：OAuth state 现在必须携带目标渠道名，授权成功后才能注册到正确的 provider name 下。
    # 修改方式：authorize 新增 provider query 参数，pending flow 中保存 provider 并由 callback/exchange 复用。
    # 目的：让同一个 OAuth 类型的多个渠道可以独立完成网页登录和凭据保存。
    channel_id = _require_provider_name(provider)
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)

    oauth_mgr = request.app.state.oauth_manager
    oauth_provider = oauth_mgr._providers.get(type)
    if not oauth_provider:
        return JSONResponse({"error": f"Unknown OAuth type: {type}"}, status_code=400)

    provider_mode = getattr(oauth_provider, "redirect_mode", "auto")
    effective_mode = mode or provider_mode
    if effective_mode not in {"auto", "manual"}:
        return JSONResponse({"error": f"Unsupported OAuth redirect mode: {effective_mode}"}, status_code=400)
    if mode and effective_mode != provider_mode:
        return JSONResponse(
            {"error": f"OAuth type {type} does not support {effective_mode} mode"},
            status_code=400,
        )

    state = _encode_oauth_state(channel_id)
    if effective_mode == "manual":
        # 修改原因：manual 模式必须使用 provider 白名单内的 localhost callback，否则授权服务会拒绝 redirect_uri。
        # 修改方式：优先使用路由覆盖表，其次使用 provider.localhost_redirect_uri，最后回退到基类默认值。
        # 目的：让用户登录后复制 localhost 失败页 URL，再由前端提交给 /v1/oauth/exchange。
        redirect_uri = OAUTH_REDIRECT_URIS.get(
            type,
            getattr(oauth_provider, "localhost_redirect_uri", "http://localhost:8080/callback"),
        )
    else:
        # 修改原因：auto 模式需要 provider 直接回跳 Zoaholic 后端 callback 端点。
        # 修改方式：从当前请求和代理头动态生成 {scheme}://{host}/v1/oauth/callback。
        # 目的：后端收到回调后可以直接换 token，并通过成功页 postMessage 通知管理前端。
        if origin and origin.startswith("http"):
            redirect_uri = f"{origin.rstrip('/')}/v1/oauth/callback"
        else:
            redirect_uri = _build_redirect_uri(request)

    auth_url, verifier = oauth_provider.build_auth_url(state, redirect_uri)
    created_at = time.time()
    _pending_flows[state] = {
        "type": type,
        "provider": channel_id,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "created_at": created_at,
        "mode": effective_mode,
    }
    _cleanup_expired_flows(created_at)
    return {"auth_url": auth_url, "state": state, "mode": effective_mode}


@router.post("/v1/oauth/exchange", dependencies=[Depends(verify_admin_api_key)])
async def oauth_exchange(request: Request):
    """前端捕获 OAuth code 后，调此端点完成 token 交换。"""
    # 修改原因：manual 模式的 localhost 回调只发生在用户浏览器本机，Zoaholic 后端无法直接接收该回调。
    # 修改方式：由前端解析用户粘贴的完整回调 URL，再带管理员凭据和 provider name 调用本端点完成 token 交换。
    # 目的：既满足固定 localhost redirect_uri 白名单，又保持后端统一按渠道保存 token 和注册 OAuth 账号。
    body = await request.json()
    code = body.get("code")
    state = body.get("state")
    channel_id = _require_provider_name(body.get("provider"))
    if not code or not state:
        return JSONResponse({"error": "code and state are required"}, status_code=400)
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)

    # 修改原因：state 已编码 provider，manual exchange 还会从请求体收到 provider，二者必须一致。
    # 修改方式：先尝试从 state 解出 provider，与 body.provider 不一致时直接拒绝。
    # 目的：避免用户粘贴其他渠道授权回调后，把凭据写入当前渠道。
    state_provider = _decode_oauth_state_provider(state)
    if state_provider and state_provider != channel_id:
        return JSONResponse({"error": "provider does not match authorization state"}, status_code=400)

    flow = _pending_flows.get(state)
    if not flow:
        return JSONResponse({"error": "Invalid or expired state"}, status_code=400)
    if channel_id != flow.get("provider"):
        return JSONResponse({"error": "provider does not match authorization state"}, status_code=400)

    if time.time() - flow.get("created_at", 0) > OAUTH_FLOW_TTL_SECONDS:
        _pending_flows.pop(state, None)
        return JSONResponse({"error": "Authorization timed out"}, status_code=400)

    _pending_flows.pop(state, None)
    oauth_mgr = request.app.state.oauth_manager
    oauth_provider = oauth_mgr._providers.get(flow["type"])
    if not oauth_provider:
        return JSONResponse({"error": f"Unknown OAuth type: {flow['type']}"}, status_code=400)

    try:
        # 修改原因：授权码交换需要使用最新 token_url，直接调用 provider 容易绕过 OAuthManager 的运行时配置注入。
        # 修改方式：优先调用 oauth_mgr.exchange_code 并传入 channel_id；测试替身或旧 manager 不支持时再回退到 provider.exchange_code。
        # 目的：让生产路径通过 manager 传入当前 app.state.config，同时保持轻量单元测试兼容。
        if hasattr(oauth_mgr, "exchange_code"):
            token_data = await oauth_mgr.exchange_code(
                channel_id=channel_id,
                type_name=flow["type"],
                code=code,
                redirect_uri=flow["redirect_uri"],
                code_verifier=flow["verifier"],
            )
        else:
            token_data = await oauth_provider.exchange_code(
                code=code,
                redirect_uri=flow["redirect_uri"],
                code_verifier=flow["verifier"],
            )
    except Exception as exc:
        return JSONResponse({"error": f"Token exchange failed: {exc}"}, status_code=500)

    # 修改原因：渠道 api_keys 需要保存稳定可读的账号标识，Codex token 中通常能解析出邮箱。
    # 修改方式：优先使用 token_data.email；缺失时生成 oauth_ 前缀的短随机标识，并按 channel_id 注册。
    # 目的：让前端 manual exchange 登录和 auto callback 登录都能注册为当前渠道下的 OAuthManager key_id。
    email = str(token_data.get("email") or "").strip()
    key_id = email or f"oauth_{secrets.token_hex(4)}"
    already_exists = _key_exists_in_provider(request.app, channel_id, key_id)
    await oauth_mgr.register(channel_id, key_id, flow["type"], token_data)
    return {"message": "Account registered", "key_id": key_id, "already_exists": already_exists}


@router.get("/v1/oauth/callback")
async def oauth_callback(code: str, state: str, request: Request):
    """OAuth 回调，换取 token 并注册账号。"""
    # 修改原因：OAuth provider 的浏览器回跳无法携带管理员 Authorization header。
    # 修改方式：callback 不加 admin key 依赖，只接受 authorize 阶段生成并保存了 provider 的 state。
    # 目的：既允许浏览器完成 OAuth 回跳，又保持最小 CSRF、过期保护和分渠道注册。
    flow = _pending_flows.pop(state, None)
    if not flow:
        return _oauth_error_page("授权失败", "无效或过期的 state 参数。请重新发起登录。", status_code=400)

    if time.time() - float(flow.get("created_at", 0)) > OAUTH_FLOW_TTL_SECONDS:
        return _oauth_error_page("授权失败", "授权超时，请重新发起登录。", status_code=400)

    # 修改原因：OAuth callback 只有 state 可用于恢复渠道名，新 state 会编码 provider，pending flow 仍保留兼容副本。
    # 修改方式：优先读取 pending flow 中的 provider，再用 state 中的 provider 交叉校验或兜底。
    # 目的：防止 state 与服务端 flow 渠道不一致时继续交换 token。
    state_provider = _decode_oauth_state_provider(state)
    channel_id = _require_provider_name(flow.get("provider") or state_provider)
    if state_provider and channel_id and state_provider != channel_id:
        return _oauth_error_page("授权失败", "state 中的 provider 与授权流程不一致。请重新发起登录。", status_code=400)
    if not channel_id:
        return _oauth_error_page("授权失败", "授权流程缺少 provider 信息，请重新发起登录。", status_code=400)

    oauth_mgr = request.app.state.oauth_manager
    oauth_provider = oauth_mgr._providers.get(flow["type"])
    if not oauth_provider:
        return _oauth_error_page("授权失败", f"未知 OAuth 类型: {flow['type']}", status_code=400)

    try:
        # 修改原因：授权码交换需要使用最新 token_url，直接调用 provider 容易绕过 OAuthManager 的运行时配置注入。
        # 修改方式：优先调用 oauth_mgr.exchange_code 并传入 channel_id；测试替身或旧 manager 不支持时再回退到 provider.exchange_code。
        # 目的：让生产路径通过 manager 传入当前 app.state.config，同时保持轻量单元测试兼容。
        if hasattr(oauth_mgr, "exchange_code"):
            token_data = await oauth_mgr.exchange_code(
                channel_id=channel_id,
                type_name=flow["type"],
                code=code,
                redirect_uri=flow["redirect_uri"],
                code_verifier=flow["verifier"],
            )
        else:
            token_data = await oauth_provider.exchange_code(
                code=code,
                redirect_uri=flow["redirect_uri"],
                code_verifier=flow["verifier"],
            )
    except Exception as exc:
        return _oauth_error_page("授权失败", f"换取 token 出错: {exc}", status_code=500)

    # 修改原因：渠道 api_keys 需要保存稳定可读的账号标识，Codex token 中通常能解析出邮箱。
    # 修改方式：优先使用 token_data.email；缺失时生成 oauth_ 前缀的短随机标识，并按 pending flow 中的 provider 注册。
    # 目的：让 callback 登录和手动导入都能最终注册为当前渠道下可解析的 key_id。
    email = str(token_data.get("email") or "").strip()
    key_id = email or f"oauth_{secrets.token_hex(4)}"
    already_exists = _key_exists_in_provider(request.app, channel_id, key_id)
    await oauth_mgr.register(channel_id, key_id, flow["type"], token_data)
    return _oauth_success_page(key_id, state, channel_id, already_exists=already_exists)


@router.post("/v1/oauth/import", dependencies=[Depends(verify_admin_api_key)])
async def import_account(request: Request):
    """手动导入 OAuth 账号。"""
    body = await request.json()
    channel_id = _require_provider_name(body.get("provider"))
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)
    type_name = body["type"]
    oauth_mgr = request.app.state.oauth_manager
    oauth_provider = oauth_mgr._providers.get(type_name)

    # 修改原因：Vertex 等渠道可以直接使用 Google service account JSON，不应要求管理员手动拆分字段。
    # 修改方式：当请求体包含 service_account_json 时，先解析 JSON，校验 client_email 和 private_key，再按 OAuthManager 的分渠道结构注册。
    # 目的：支持 service account 导入，同时保留 project_id、email 等后续 adapter 需要的非敏感元数据。
    sa = body.get("service_account_json")
    if sa is not None:
        if isinstance(sa, str):
            try:
                sa = json.loads(sa)
            except Exception:
                return JSONResponse({"error": "service_account_json must be valid JSON"}, status_code=400)
        if not isinstance(sa, dict):
            return JSONResponse({"error": "service_account_json must be a JSON object"}, status_code=400)

        client_email = sa.get("client_email", "").strip()
        private_key = sa.get("private_key", "")
        project_id = sa.get("project_id", "").strip()

        if not client_email or not private_key:
            return JSONResponse({"error": "service_account_json missing client_email or private_key"}, status_code=400)

        token_data = {
            "client_email": client_email,
            "private_key": private_key,
            "email": client_email,
        }
        if project_id:
            token_data["project_id"] = project_id

        if oauth_provider:
            try:
                updated = await oauth_provider.refresh_token(token_data)
                await oauth_mgr.register(channel_id, client_email, type_name, updated)
                return {"message": "Service account imported", "key_id": client_email}
            except Exception as e:
                return JSONResponse({"error": f"Failed to verify service account: {e}"}, status_code=400)
        else:
            await oauth_mgr.register(channel_id, client_email, type_name, token_data)
            return {"message": "Service account imported (unverified)", "key_id": client_email}

    key_id = body["key_id"]
    token_data = _token_data_from_body(body)
    try:
        if body.get("refresh_token") and oauth_provider:
            if hasattr(oauth_mgr, "refresh_provider"):
                updated = await oauth_mgr.refresh_provider(type_name, token_data)
            else:
                updated = await oauth_provider.refresh_token(token_data)
            email = updated.get("email")
            final_key_id = email if email else key_id
            already_exists = _key_exists_in_provider(request.app, channel_id, final_key_id)
            await oauth_mgr.register(channel_id, final_key_id, type_name, updated)
            return {"message": "Account imported", "key_id": final_key_id, "already_exists": already_exists}
        else:
            already_exists = _key_exists_in_provider(request.app, channel_id, key_id)
            await oauth_mgr.register(channel_id, key_id, type_name, token_data)
            return {"message": "Account imported", "key_id": key_id, "already_exists": already_exists}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/v1/oauth/batch_import", dependencies=[Depends(verify_admin_api_key)])
async def batch_import(request: Request):
    """批量导入 OAuth 账号，兼容 sub2api 和 CPA 导出格式。"""
    # 修改原因：现有 /v1/oauth/import 只能导入单个账号，CPA/sub2api 迁移时需要手动拆分大量凭据。
    # 修改方式：新增批量端点，先自动 normalize 三种导出格式，再逐条刷新、注册和记录结果。
    # 目的：让管理员可以一次导入多账号，同时单个账号失败不会中断整批处理。
    body = await request.json()
    channel_id = _require_provider_name(body.get("provider"))
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)
    type_name = str(body.get("type") or "").strip()
    if not type_name:
        return JSONResponse({"error": "type is required"}, status_code=400)
    if "data" not in body:
        return JSONResponse({"error": "data is required"}, status_code=400)

    try:
        normalized_items = _normalize_batch_import_data(body.get("data"))
    except Exception as exc:
        return JSONResponse({"error": f"Failed to parse batch import data: {exc}"}, status_code=400)

    oauth_mgr = request.app.state.oauth_manager
    oauth_provider = oauth_mgr._providers.get(type_name)
    results = []
    success = 0
    failed = 0
    skipped = 0

    for item in normalized_items:
        # 修改原因：批量导入中的任一条数据都可能缺字段或刷新失败，不能影响其他账号继续导入。
        # 修改方式：每个账号独立 try/except，逐条生成 status、already_exists 或 error。
        # 目的：返回完整的导入报告，方便前端或管理员定位具体失败账号。
        key_id = _first_non_empty_string(item.get("key_id")) or "unknown"
        token_data = item.get("token_data") if isinstance(item.get("token_data"), dict) else {}
        token_data = dict(token_data)

        try:
            if token_data.get("refresh_token") and oauth_provider:
                # 修改原因：CPA/sub2api 导出中的 access_token 可能已经临近过期，refresh_token 可用时应先刷新再落库。
                # 修改方式：优先走 OAuthManager.refresh_provider 以继承运行时配置注入；旧 manager 或测试替身缺失该方法时回退到 provider.refresh_token。
                # 目的：保存更新后的 access_token 和可能轮换后的 refresh_token，减少导入后首次请求失败概率。
                if hasattr(oauth_mgr, "refresh_provider"):
                    token_data = await oauth_mgr.refresh_provider(type_name, token_data)
                else:
                    token_data = await oauth_provider.refresh_token(token_data)

            if not token_data.get("access_token"):
                skipped += 1
                results.append({"key_id": key_id, "status": "skipped", "error": "missing access_token"})
                continue

            if not token_data.get("refresh_token") and _batch_token_is_expired(token_data):
                skipped += 1
                results.append({"key_id": key_id, "status": "skipped", "error": "token expired"})
                continue

            if token_data.get("refresh_token") and not oauth_provider and _batch_token_is_expired(token_data):
                skipped += 1
                results.append({"key_id": key_id, "status": "skipped", "error": "token expired and refresh unavailable"})
                continue

            email = _first_non_empty_string(token_data.get("email"))
            final_key_id = email or key_id
            already_exists = _key_exists_in_provider(request.app, channel_id, final_key_id)
            await oauth_mgr.register(channel_id, final_key_id, type_name, token_data)
            success += 1
            results.append({"key_id": final_key_id, "status": "success", "already_exists": already_exists})
        except Exception as exc:
            failed += 1
            results.append({"key_id": key_id, "status": "failed", "error": str(exc)})

    return {
        "total": len(normalized_items),
        "success": success,
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


@router.get("/v1/oauth/accounts", dependencies=[Depends(verify_admin_api_key)])
async def list_accounts(request: Request, provider: str | None = None):
    """列出已导入的 OAuth 账号。"""
    # 修改原因：账号列表既要支持全量查看，也要支持前端编辑单个渠道时只取当前 provider。
    # 修改方式：query provider 可选；传入时返回该渠道扁平账号表，不传时返回全部渠道的嵌套账号表。
    # 目的：让前端避免加载其他渠道同邮箱账号，也保留管理员排查全量状态的能力。
    channel_id = _require_provider_name(provider) if provider is not None else None
    return request.app.state.oauth_manager.list_accounts(channel_id=channel_id)


@router.get("/v1/oauth/accounts/{key_id}/quota", dependencies=[Depends(verify_admin_api_key)])
async def get_account_quota(key_id: str, request: Request, provider: str):
    """获取 OAuth 账号的额度信息。"""
    # 修改原因：OAuth 账号额度通常需要访问上游 API，且同 key_id 可能存在于多个渠道。
    # 修改方式：强制 query provider，并把 provider/key_id 同时传给 OAuthManager.fetch_quota。
    # 目的：避免账号列表接口被网络请求拖慢，同时防止 quota 查询读到其他渠道的同名账号。
    channel_id = _require_provider_name(provider)
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)
    try:
        # 修改原因：provider 层现在会把 Claude Code usage 上游错误向上抛出，默认 500 无法给前端提供可见细节。
        # 修改方式：在路由层捕获 quota 查询异常，并返回带错误详情的 502 JSON 响应。
        # 目的：让前端可以读取错误正文，并在控制台展示可排查的失败原因。
        quota = await request.app.state.oauth_manager.fetch_quota(channel_id, key_id)
    except Exception as exc:
        return JSONResponse({"error": f"Quota query failed: {exc}"}, status_code=502)
    if quota is None:
        return JSONResponse({"error": "Quota not available"}, status_code=404)
    return quota


@router.put("/v1/oauth/accounts/{key_id}/rename", dependencies=[Depends(verify_admin_api_key)])
async def rename_account(key_id: str, request: Request):
    """重命名 OAuth 账号标识符。"""
    # 修改原因：前端编辑 OAuth key 输入框只会修改 api.yaml，必须额外同步 oauth_state.json 的字典键。
    # 修改方式：读取 body.provider 和 new_key_id 后调用 OAuthManager.rename，并把常见冲突转成明确的 JSON 状态码。
    # 目的：避免用户保存新账号标识后，运行时仍用旧 key 或其他渠道的同名 key 查找 access_token。
    body = await request.json()
    channel_id = _require_provider_name(body.get("provider"))
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)
    new_key_id = str(body.get("new_key_id", "")).strip()
    if not new_key_id:
        return JSONResponse({"error": "new_key_id is required"}, status_code=400)

    try:
        await request.app.state.oauth_manager.rename(channel_id, key_id, new_key_id)
    except ValueError as exc:
        message = str(exc)
        lowered = message.lower()
        if "not found" in lowered:
            status_code = 404
        elif "already exists" in lowered:
            status_code = 409
        else:
            status_code = 400
        return JSONResponse({"error": message}, status_code=status_code)
    # 修改原因：OAuthManager.rename 只会迁移 oauth_state.json，当前渠道 api 列表仍会引用旧账号名。
    # 修改方式：rename 成功后立即同步 app.state.config.providers 中的 api 条目，并复用配置持久化流程写回 api.yaml。
    # 目的：前端失焦改名后无需再手动保存渠道，配置文件和 OAuth state 即可保持一致。
    await _sync_provider_api_key_rename(request.app, channel_id, key_id, new_key_id)
    return {"message": "Account renamed", "old_key_id": key_id, "new_key_id": new_key_id}


@router.post("/v1/oauth/copy-provider", dependencies=[Depends(verify_admin_api_key)])
async def copy_provider_oauth_state(request: Request):
    """
    复制 OAuth state：把 source_provider 下所有账号的 OAuth state 复制到 target_provider 下。
    Body: { "source_provider": "原渠道名", "target_provider": "新渠道名" }
    """
    # 修改原因：复制渠道只复制 api.yaml 中的账号 key，不会复制 OAuthManager 内部按 provider 分层保存的 token state。
    # 修改方式：校验 source_provider 与 target_provider 后，调用 OAuthManager.copy_channel_state 做 provider 级深拷贝。
    # 目的：让复制出来的 OAuth 渠道保存后可以立即使用原渠道账号，不再因缺少 token state 返回 401。
    body = await request.json()
    source_provider = _require_provider_name(body.get("source_provider"))
    target_provider = _require_provider_name(body.get("target_provider"))
    if not source_provider:
        return JSONResponse({"error": "source_provider is required"}, status_code=400)
    if not target_provider:
        return JSONResponse({"error": "target_provider is required"}, status_code=400)

    await request.app.state.oauth_manager.copy_channel_state(source_provider, target_provider)
    return {
        "message": "OAuth state copied",
        "source_provider": source_provider,
        "target_provider": target_provider,
    }


@router.delete("/v1/oauth/accounts/{key_id}", dependencies=[Depends(verify_admin_api_key)])
async def remove_account(key_id: str, request: Request, provider: str):
    """移除已导入的 OAuth 账号。"""
    # 修改原因：删除凭据是敏感操作，必须限定 provider name，不能删除其他渠道的同邮箱账号。
    # 修改方式：query provider 必填，并传给 OAuthManager.remove。
    # 目的：让前端删除某个 OAuth Key 时只清理当前渠道状态。
    channel_id = _require_provider_name(provider)
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)
    await request.app.state.oauth_manager.remove(channel_id, key_id)
    return {"message": "Account removed"}


@router.get("/v1/oauth/export", dependencies=[Depends(verify_admin_api_key)])
async def export_credentials(provider: str, request: Request):
    """导出指定渠道的所有 OAuth 凭证（含 refresh_token，敏感操作）。"""
    # 修改原因：迁移和备份需要完整凭据，普通 list_accounts 默认会脱敏 token。
    # 修改方式：导出端点强制 provider query，并以 include_tokens=True 读取该渠道账号。
    # 目的：只在管理员显式调用导出接口时返回 refresh_token，日常列表仍保持脱敏。
    channel_id = _require_provider_name(provider)
    if not channel_id:
        return JSONResponse({"error": "provider is required"}, status_code=400)
    accounts = request.app.state.oauth_manager.list_accounts(channel_id=channel_id, include_tokens=True)
    if not accounts:
        return JSONResponse({"error": "No accounts found"}, status_code=404)

    export_data = []
    for key_id, cred in accounts.items():
        export_data.append({
            "key_id": key_id,
            "type": cred.get("type"),
            "email": cred.get("email"),
            "refresh_token": cred.get("refresh_token"),
            "access_token": cred.get("access_token"),
            "expires_at": cred.get("expires_at"),
            "status": cred.get("status"),
        })
    return {"provider": channel_id, "accounts": export_data}
