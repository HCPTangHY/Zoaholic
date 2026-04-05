"""
通用渠道余额查询引擎

根据 provider 配置中的 preferences.balance 规则，
向任意 HTTP 接口发请求，按 dot notation 路径从返回 JSON 中提取字段，
返回标准化的余额结构。

配置示例（provider.preferences.balance）:
    template: "new-api"          # 可选，使用预置模板
    endpoint: "/api/status"      # 余额接口地址（绝对 URL 或相对路径）
    method: "GET"                # 请求方法，默认 GET
    auth: "bearer"               # 认证方式：bearer / header / none
    mapping:                     # 字段提取映射（dot notation）
      total: "data.totalQuota"
      used: "data.usedQuota"
      available: "data.remainQuota"
      value_type: "'amount'"     # amount | percent
"""

import asyncio
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .log_config import logger
from .json_utils import json_loads


# ==================== 预置模板 ====================

BALANCE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "new-api": {
        "endpoint": "/api/status",
        "method": "GET",
        "auth": "bearer",
        "mapping": {
            "total": "data.totalQuota",
            "used": "data.usedQuota",
            "available": "data.remainQuota",
            "value_type": "'amount'",
        },
    },
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/key",
        "method": "GET",
        "auth": "bearer",
        "mapping": {
            "total": "data.limit",
            "used": "data.usage",
            "value_type": "'amount'",
        },
    },
}


# ==================== 工具函数 ====================


def extract_value(data: Any, path: Optional[str]) -> Any:
    """按 dot notation 从 dict 中提取值。

    - "data.totalQuota"  → data["data"]["totalQuota"]
    - "'CNY'"            → 常量字符串 "CNY"
    - None / 空串        → None
    """
    if path is None:
        return None
    if not isinstance(path, str):
        return None
    path = path.strip()
    if not path:
        return None

    # 单引号包裹 = 常量
    if path.startswith("'") and path.endswith("'") and len(path) >= 2:
        return path[1:-1]

    # dot notation 遍历
    current = data
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list):
            # 支持数字索引，如 "items.0.value"
            try:
                current = current[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _to_float(value: Any) -> Optional[float]:
    """尝试将值转为 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_balance_endpoint(base_url: str, endpoint: str) -> str:
    """将 endpoint 解析为完整 URL。

    - 绝对 URL（http/https 开头）：直接使用
    - 相对路径（/ 开头）：拼接到 base_url 的域名下（忽略 base_url 中的路径部分）
    - 其他：拼接到 base_url 末尾
    """
    endpoint = endpoint.strip()

    # 绝对 URL
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint

    # 清理 base_url 末尾的 '#'（项目中 '#' 表示固定地址的约定）
    clean_base = base_url.rstrip("#").rstrip("/")

    if endpoint.startswith("/"):
        # 相对路径：拼接到域名根路径下
        parsed = urlparse(clean_base)
        return f"{parsed.scheme}://{parsed.netloc}{endpoint}"
    else:
        # 其他情况：追加到 base_url 后面
        return f"{clean_base}/{endpoint}"


def build_balance_config(provider: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 provider 配置中解析余额查询配置。

    支持 template + 覆盖字段的合并逻辑。
    如果 preferences.balance 不存在，返回 None。
    """
    prefs = provider.get("preferences")
    if not isinstance(prefs, dict):
        return None

    balance_cfg = prefs.get("balance")
    if not balance_cfg or not isinstance(balance_cfg, dict):
        return None

    # 加载模板作为基础
    template_name = balance_cfg.get("template")
    if template_name and template_name in BALANCE_TEMPLATES:
        import copy
        merged = copy.deepcopy(BALANCE_TEMPLATES[template_name])
        # 用户配置覆盖模板
        for key, value in balance_cfg.items():
            if key == "template":
                continue
            if key == "mapping" and isinstance(value, dict):
                # mapping 做字段级合并
                if "mapping" not in merged:
                    merged["mapping"] = {}
                merged["mapping"].update(value)
            else:
                merged[key] = value
        return merged
    else:
        return dict(balance_cfg)


# ==================== 核心查询函数 ====================


async def query_provider_balance(client, provider: Dict[str, Any]) -> Dict[str, Any]:
    """通用余额查询引擎。

    Args:
        client: httpx.AsyncClient（可能被 InterceptedClient 包装）
        provider: 完整的 provider 配置 dict

    Returns:
        标准化余额结构:
        {
            "supported": bool,
            "value_type": "amount" | "percent",
            "total": float | None,
            "used": float | None,
            "available": float | None,
            "percent": float | None,
            "expires_at": str | None,
            "raw": dict | None,
            "error": str | None,
        }
    """
    # 解析配置
    balance_cfg = build_balance_config(provider)
    if not balance_cfg:
        return {
            "supported": False,
            "error": "该渠道未配置余额查询（preferences.balance）",
        }

    endpoint = balance_cfg.get("endpoint", "").strip()
    if not endpoint:
        return {
            "supported": False,
            "error": "余额查询配置缺少 endpoint",
        }

    method = balance_cfg.get("method", "GET").upper()
    auth_mode = balance_cfg.get("auth", "bearer").lower()
    mapping = balance_cfg.get("mapping") or {}

    # 拼接 URL
    base_url = provider.get("base_url", "")
    url = resolve_balance_endpoint(base_url, endpoint)

    # 构造 headers
    headers = {"Content-Type": "application/json"}

    api_key = provider.get("api") or provider.get("api_key") or ""
    if isinstance(api_key, list):
        api_key = api_key[0] if api_key else ""

    if auth_mode == "bearer" and api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_mode == "header" and api_key:
        # 某些站用其他 header（如 x-api-key），但大多数情况 bearer 足够
        headers["Authorization"] = f"Bearer {api_key}"

    # 发送请求
    try:
        if method == "POST":
            response = await client.post(url, headers=headers, timeout=15)
        else:
            response = await client.get(url, headers=headers, timeout=15)

        response.raise_for_status()
    except Exception as e:
        error_msg = str(e)
        status_code = None
        # 尝试提取上游返回的错误信息
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                status_code = resp.status_code
            except Exception:
                pass
            try:
                error_msg = resp.text[:500]
            except Exception:
                pass

        logger.warning(f"Balance query failed for {url}: status={status_code}, error={error_msg}")
        return {
            "supported": True,
            "error": f"请求余额接口失败: {error_msg}"[:500],
            "raw": None,
        }

    # 解析响应
    try:
        raw_data = response.json()
    except Exception:
        # 兜底：部分接口返回 SSE 格式 "data:{...}" 或带前缀文本
        raw_text = response.text.strip()
        raw_data = None
        # 尝试逐行查找可解析的 JSON
        for line in raw_text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    raw_data = json_loads(line)
                    break
                except Exception:
                    continue
        if raw_data is None:
            return {
                "supported": True,
                "error": f"余额接口返回的不是有效 JSON: {raw_text[:200]}",
                "raw": None,
            }

    # 提取字段
    value_type = extract_value(raw_data, mapping.get("value_type")) or "amount"

    result: Dict[str, Any] = {
        "supported": True,
        "value_type": value_type,
        "total": None,
        "used": None,
        "available": None,
        "percent": None,
        "expires_at": None,
        "raw": raw_data,
        "error": None,
    }

    if value_type == "percent":
        raw_percent = _to_float(
            extract_value(raw_data, mapping.get("percent"))
            or extract_value(raw_data, mapping.get("available"))
        )
        if raw_percent is not None:
            multiplier = _to_float(balance_cfg.get("percent_multiplier")) or 1
            result["percent"] = raw_percent * multiplier
    else:
        result["total"] = _to_float(extract_value(raw_data, mapping.get("total")))
        result["used"] = _to_float(extract_value(raw_data, mapping.get("used")))
        result["available"] = _to_float(extract_value(raw_data, mapping.get("available")))

        # 自动补全第三个值
        if result["total"] is not None and result["used"] is not None and result["available"] is None:
            result["available"] = result["total"] - result["used"]
        elif result["total"] is not None and result["available"] is not None and result["used"] is None:
            result["used"] = result["total"] - result["available"]
        elif result["used"] is not None and result["available"] is not None and result["total"] is None:
            result["total"] = result["used"] + result["available"]

    result["expires_at"] = extract_value(raw_data, mapping.get("expires_at"))

    return result


def list_balance_templates() -> Dict[str, Dict[str, Any]]:
    """返回所有预置模板（供前端展示选择）。"""
    return {name: dict(tpl) for name, tpl in BALANCE_TEMPLATES.items()}
