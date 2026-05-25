"""
oai_tier — OpenAI 官方渠道 Tier 检测（被动 + 主动）

被动模式：通过响应头 x-ratelimit-limit-tokens 采集 TPM 上限。
主动模式：余额查询时缓存无数据，发 max_completion_tokens:0 探测请求从响应头采集（零 token 消耗）。
推断 OpenAI Tier 等级，并通过 balance_enricher 注入到余额查询结果中。

使用方式：
  在 provider 的 enabled_plugins 里加 "oai_tier"
  仅给 api.openai.com 的渠道启用，不要给 Azure 或其他兼容站启用
"""

from typing import Any, Dict, Optional
import json
import time

from core.log_config import logger
from core.plugins import (
    register_response_interceptor,
    unregister_response_interceptor,
    register_balance_enricher,
    unregister_balance_enricher,
)


PLUGIN_META = {
    "name": "oai_tier",
    "display_name": "OpenAI Tier 检测",
    "description": "检测 OpenAI 账户 Tier 等级（被动采集 + 主动探测）",
    "version": "1.0.0",
    "category": "interceptors",
}

PLUGIN_EXPORTS = [
    "interceptors:oai_tier_response",
    "balance_enrichers:oai_tier_balance",
]

# 修改原因：当前插件加载器读取 PLUGIN_INFO 和 EXTENSIONS，任务说明中的 PLUGIN_META/PLUGIN_EXPORTS 需要兼容现有加载协议。
# 修改方式：保留 PLUGIN_META/PLUGIN_EXPORTS，同时提供同内容的 PLUGIN_INFO/EXTENSIONS 别名。
# 目的：让插件既符合本次命名要求，又能在现有插件管理器中展示元信息和扩展声明。
PLUGIN_INFO = {
    "name": PLUGIN_META["name"],
    "version": PLUGIN_META["version"],
    "description": PLUGIN_META["description"],
    "author": "Zoaholic Team",
    "dependencies": [],
    "metadata": {
        "display_name": PLUGIN_META["display_name"],
        "category": PLUGIN_META["category"],
        "tags": ["openai", "tier", "rate-limit", "balance"],
    },
}
EXTENSIONS = PLUGIN_EXPORTS


# 修改原因：oai_tier 的 tier 标签不能继续由 Channels.tsx 按 bal?.tier 硬编码读取。
# 修改方式：把 tier 与百分比展示封装成 quota_display 内联 JS，由插件 setup 注册到独立 UI slot 注册表。
# 目的：让普通 OpenAI Key 的 tier 展示跟随插件生命周期，并为后续 provider 级 slot 解析做准备。
OAI_TIER_QUOTA_DISPLAY = """
export default function render(ctx) {
    const { el, data } = ctx || {};
    if (!el) return;
    
    const tier = data?.tier;
    const percent = typeof data?.percent === 'number' ? data.percent : null;
    const available = data?.available;
    const total = data?.total;
    
    // 构建显示内容
    const parts = [];
    if (tier) parts.push(tier);
    if (percent != null) parts.push(`${percent.toFixed(1)}%`);
    else if (available != null && total != null && total > 0) {
        parts.push(`${(available / total * 100).toFixed(1)}%`);
    }
    
    if (!parts.length) { el.textContent = ''; return; }
    
    const text = parts.join(' ');
    el.textContent = text;
    
    // 颜色
    const pct = percent ?? (available != null && total != null && total > 0 ? available / total * 100 : null);
    const colorClass = pct == null ? 'text-blue-400 bg-blue-500/12'
        : pct >= 50 ? 'text-emerald-500 bg-emerald-500/15'
        : pct >= 20 ? 'text-amber-600 bg-amber-500/15'
        : 'text-red-500 bg-red-500/15';
    el.className = `text-[10px] font-semibold font-mono px-1.5 py-0.5 rounded ${colorClass}`;
}
"""


# ==================== Tier 缓存 ====================

# key: api_key 前 12 位, value: {"tier": str, "tpm": int, "rpm": int, "updated_at": float}
_tier_cache: Dict[str, Dict[str, Any]] = {}

# 主动探测防抖: key_prefix -> last_probe_timestamp
_probe_timestamps: Dict[str, float] = {}
_PROBE_COOLDOWN = 300  # 5 分钟内不重复探测同一个 key

# TPM → Tier 精确映射表 (来自 keychecker/OpenAI.py)
# 注意：Tier 5 的 TPM 取决于探测模型 — gpt-5 系列为 40M，其他为 30M
_OAI_TIERS = {
    'Tier 1': {'tpm': 500_000, 'rpm': 500},
    'Tier 2': {'tpm': 1_000_000, 'rpm': 5_000},
    'Tier 3': {'tpm': 2_000_000, 'rpm': 5_000},
    'Tier 4': {'tpm': 4_000_000, 'rpm': 10_000},
    'Tier 5': {'tpm': 40_000_000, 'rpm': 15_000},  # gpt-5 系列; 非 gpt-5 → 30M tpm, 10k rpm
}

# 探测模型优先级：gpt-5.5 的限制最能反映真实 tier
_TIER_MODEL_PRIORITY = [
    "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5",
    "o3", "gpt-4.1", "chatgpt-4o-latest", "gpt-4o",
]


def _tpm_to_tier(tpm: int, model_name: str = "") -> str:
    """根据 TPM 上限精确匹配 Tier（与 keychecker 一致）"""
    if not isinstance(tpm, int):
        return "Tier Unknown"
    import copy
    fixed_tiers = copy.deepcopy(_OAI_TIERS)
    # 非 gpt-5 系列的 Tier 5 TPM 是 30M 不是 40M
    if "gpt-5" not in model_name:
        fixed_tiers['Tier 5']['tpm'] = 30_000_000
    for tier_name, tier_data in fixed_tiers.items():
        if tier_data['tpm'] == tpm:
            return tier_name
    return "Tier Unknown"


def _cache_key(api_key: str) -> str:
    """取 key 前 12 位做缓存索引"""
    return (api_key or "")[:12]


# ==================== Response Interceptor ====================

async def _oai_tier_response_interceptor(response_chunk, engine, model, is_stream):
    """从响应头被动采集 TPM/RPM"""
    try:
        from core.middleware import request_info
        info = request_info.get()
        if not info:
            return response_chunk

        headers_json = info.get("upstream_response_headers")
        if not headers_json:
            return response_chunk

        headers = json.loads(headers_json) if isinstance(headers_json, str) else headers_json

        # 修改原因：OpenAI 官方响应头中只需要 rate-limit 上限即可被动推断账户 tier。
        # 修改方式：大小写不敏感地读取 x-ratelimit-limit-tokens 和 x-ratelimit-limit-requests。
        # 目的：不额外请求 OpenAI API，也能在正常模型调用后缓存 TPM/RPM 信息。
        tpm_str = None
        rpm_str = None
        for k, v in headers.items():
            kl = k.lower()
            if kl == "x-ratelimit-limit-tokens":
                tpm_str = v
            elif kl == "x-ratelimit-limit-requests":
                rpm_str = v

        if not tpm_str:
            return response_chunk

        tpm = int(str(tpm_str).replace(",", ""))
        rpm = int(str(rpm_str).replace(",", "")) if rpm_str else None
        tier = _tpm_to_tier(tpm, model or "")

        # 修改原因：余额查询阶段拿不到上一次响应头，需要跨请求暂存被动检测结果。
        # 修改方式：用 API Key 前 12 位作为缓存索引，保存 tier、TPM、RPM、模型和更新时间。
        # 目的：后续 balance_enricher 可以按同一个 Key 把 tier 注入余额查询结果。
        api_key = info.get("_used_api_key") or ""
        ck = _cache_key(api_key)
        if ck:
            _tier_cache[ck] = {
                "tier": tier,
                "tpm": tpm,
                "rpm": rpm,
                "model": model,
                "updated_at": time.time(),
            }
            logger.debug(f"[oai_tier] Cached tier for {ck}***: {tier} (TPM={tpm})")

    except Exception as e:
        logger.debug(f"[oai_tier] Error in response interceptor: {e}")

    return response_chunk


# ==================== Active Probe ====================

async def _probe_tier(api_key: str, base_url: str = "") -> Optional[Dict[str, Any]]:
    """主动探测 tier：先查 /models 选最优探测模型，再发请求从响应头采集 TPM/RPM"""
    import httpx

    if not base_url:
        base_url = "https://api.openai.com/v1"
    base = base_url.rstrip('/')
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. 先拉模型列表，按优先级选探测模型
            probe_model = "gpt-4o-mini"  # fallback
            try:
                models_resp = await client.get(f"{base}/models", headers=auth_headers)
                if models_resp.status_code == 200:
                    available = {m["id"] for m in models_resp.json().get("data", [])}
                    for preferred in _TIER_MODEL_PRIORITY:
                        if preferred in available:
                            probe_model = preferred
                            break
                # 403 = 无 /models 权限，用 fallback
            except Exception:
                pass

            # 2. 发探测请求（keychecker 用 max_tokens for gpt-4 系, max_completion_tokens for others）
            param = "max_tokens" if "gpt-4" in probe_model else "max_completion_tokens"
            payload = {
                "model": probe_model,
                "messages": [{"role": "user", "content": ""}],
                param: 0,
            }
            resp = await client.post(f"{base}/chat/completions", json=payload, headers=auth_headers)

        tpm_str = resp.headers.get("x-ratelimit-limit-tokens")
        rpm_str = resp.headers.get("x-ratelimit-limit-requests")

        if not tpm_str:
            return None

        tpm = int(str(tpm_str).replace(",", ""))
        rpm = int(str(rpm_str).replace(",", "")) if rpm_str else None
        tier = _tpm_to_tier(tpm, probe_model)

        return {
            "tier": tier,
            "tpm": tpm,
            "rpm": rpm,
            "model": f"{probe_model} (probe)",
            "updated_at": time.time(),
        }
    except Exception as e:
        logger.debug(f"[oai_tier] Probe failed: {e}")
        return None


# ==================== Balance Enricher ====================

async def _oai_tier_balance_enricher(result: dict, engine: str, provider: dict) -> dict:
    """往 balance 结果里补充 tier 信息"""
    try:
        # 修改原因：provider.api 在不同入口可能是字符串、列表或单键 dict，缓存查找必须统一成真实 Key 字符串。
        # 修改方式：依次兼容 api、api_key、列表首项和单键 dict 的常见形态。
        # 目的：确保逐 Key 余额查询和旧配置格式都能匹配到 response_interceptor 写入的缓存。
        api_key = provider.get("api") or provider.get("api_key") or ""
        if isinstance(api_key, list):
            api_key = api_key[0] if api_key else ""
        if isinstance(api_key, dict) and len(api_key) == 1:
            api_key = str(next(iter(api_key.keys())))

        ck = _cache_key(str(api_key))
        cached = _tier_cache.get(ck)

        # 缓存未命中，主动探测
        if not cached and ck:
            now = time.time()
            if now - _probe_timestamps.get(ck, 0) > _PROBE_COOLDOWN:
                _probe_timestamps[ck] = now
                base_url = provider.get("base_url") or ""
                probed = await _probe_tier(str(api_key), base_url)
                if probed:
                    _tier_cache[ck] = probed
                    cached = probed
                    logger.info(f"[oai_tier] Probed tier for {ck}***: {probed['tier']} (TPM={probed['tpm']})")

        if cached:
            result["tier"] = cached["tier"]
            result["tpm"] = cached.get("tpm")
            result["rpm"] = cached.get("rpm")
            result["tier_detected_at"] = cached.get("updated_at")
            result["tier_model"] = cached.get("model")
    except Exception as e:
        logger.debug(f"[oai_tier] Error in balance enricher: {e}")

    return result


# ==================== 生命周期 ====================

def setup(manager):
    """插件加载"""
    from core.ui_slots.registry import register_ui_slot

    # 修改原因：Tier 检测分为响应头采集、余额结果补充和前端展示三个阶段，不能写入 oai_tools 插件或 Channels.tsx。
    # 修改方式：注册 response_interceptor 采集 OpenAI rate-limit 头，注册 balance_enricher 输出缓存字段，并注册 quota_display UI slot。
    # 目的：使 oai_tier 只在渠道启用时工作，并与其他 OpenAI 兼容渠道插件解耦。
    register_response_interceptor(
        interceptor_id="oai_tier_response",
        callback=_oai_tier_response_interceptor,
        priority=200,
        plugin_name="oai_tier",
        overwrite=True,
    )
    register_balance_enricher(
        enricher_id="oai_tier_balance",
        callback=_oai_tier_balance_enricher,
        priority=100,
        plugin_name="oai_tier",
        overwrite=True,
    )
    # 修改原因：前端普通 Key 行不应继续硬编码读取 balanceResults.tier。
    # 修改方式：注册一个仅匹配 OpenAI API Key 渠道且要求 provider 启用 oai_tier 的 quota_display 贡献。
    # 目的：让 tier 展示由插件声明，并避免 OAuth 渠道或未启用插件的 provider 误加载该脚本。
    register_ui_slot(
        slot_id="oai_tier.quota_display",
        slot="quota_display",
        script=OAI_TIER_QUOTA_DISPLAY,
        source="oai_tier",
        priority=50,
        engines=["openai", "openai-responses"],
        auth_types=["api_key"],
        enabled_plugin="oai_tier",
    )
    logger.info("[oai_tier] Plugin loaded")


def teardown(manager):
    """插件卸载"""
    from core.ui_slots.registry import unregister_ui_slot

    unregister_response_interceptor("oai_tier_response")
    unregister_balance_enricher("oai_tier_balance")
    # 修改原因：插件卸载后前端不应继续拿到 oai_tier 的 quota_display 脚本。
    # 修改方式：按 setup 中使用的固定 slot_id 从独立 UI slot 注册表注销贡献。
    # 目的：保证插件热重载和停用后不会留下过期 UI slot。
    unregister_ui_slot("oai_tier.quota_display")
    _tier_cache.clear()
    _probe_timestamps.clear()
    logger.info("[oai_tier] Plugin unloaded")


def unload():
    teardown(None)
