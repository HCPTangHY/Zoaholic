"""
Stats 统计和使用量路由
"""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Literal, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, Query, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_serializer, Field

from sqlalchemy import select, case, func, desc, update, delete, or_

from db import RequestStat, ChannelStat, async_session_scope, DISABLE_DATABASE, DB_TYPE
from core.stats import get_usage_data, compute_total_cost_from_db
from utils import safe_get, query_channel_key_stats
from routes.deps import rate_limit_dependency, verify_api_key, verify_admin_api_key, get_app
from core.d1_client import parse_d1_datetime, format_d1_datetime

router = APIRouter()


def _bool_from_db(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


# ============ Pydantic Models ============

class TokenUsageEntry(BaseModel):
    api_key_prefix: str
    model: str
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    request_count: int


class QueryDetails(BaseModel):
    model_config = {'protected_namespaces': ()}

    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None
    api_key_filter: Optional[str] = None
    model_filter: Optional[str] = None
    credits: Optional[str] = None
    total_cost: Optional[str] = None
    balance: Optional[str] = None


class TokenUsageResponse(BaseModel):
    usage: List[TokenUsageEntry]
    query_details: QueryDetails


class ChannelKeyRanking(BaseModel):
    api_key: str
    success_count: int
    total_requests: int
    success_rate: float
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0


class ChannelKeyRankingsResponse(BaseModel):
    rankings: List[ChannelKeyRanking]
    query_details: QueryDetails


class TokenInfo(BaseModel):
    api_key_prefix: str
    model: str
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    request_count: int


class ApiKeyState(BaseModel):
    credits: float
    created_at: datetime
    all_tokens_info: List[Dict[str, Any]]
    total_cost: float
    enabled: bool

    @field_serializer('created_at')
    def serialize_dt(self, dt: datetime):
        return dt.isoformat()


class ApiKeysStatesResponse(BaseModel):
    api_keys_states: Dict[str, ApiKeyState]


class QuotaResetRequest(BaseModel):
    api_key: str
    status_key: str


class LogEntry(BaseModel):
    id: int
    timestamp: datetime
    endpoint: Optional[str] = None
    client_ip: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key_prefix: Optional[str] = None
    process_time: Optional[float] = None
    first_response_time: Optional[float] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    # Prompt Caching 统计随日志返回给前端，用于列表摘要和展开详情展示。
    cached_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None
    success: bool = False
    status_code: Optional[int] = None
    prompt_price: Optional[float] = None
    completion_price: Optional[float] = None
    is_flagged: bool = False
    
    # 扩展日志字段
    provider_id: Optional[str] = None
    provider_key_index: Optional[int] = None
    api_key_name: Optional[str] = None
    api_key_group: Optional[str] = None
    retry_count: Optional[int] = None
    retry_path: Optional[str] = None  # JSON格式的重试路径
    request_headers: Optional[str] = None  # 用户请求头
    request_body: Optional[str] = None  # 用户请求体
    # 修改原因：前端日志详情需要展示上游请求头和上游响应头，响应模型必须显式暴露这些字段。
    # 修改方式：在 LogEntry 中补齐 upstream_request_headers 与 upstream_response_headers。
    # 目的：避免 response_model 过滤掉数据库中已经保存的头信息。
    upstream_request_headers: Optional[str] = None  # 发送到上游的请求头
    upstream_request_body: Optional[str] = None  # 发送到上游的请求体
    upstream_response_headers: Optional[str] = None  # 上游返回的响应头
    upstream_response_body: Optional[str] = None  # 上游返回的响应体
    response_body: Optional[str] = None  # 返回给用户的响应体
    raw_data_expires_at: Optional[datetime] = None  # 原始数据过期时间

    @field_serializer("timestamp")
    def serialize_dt(self, dt: datetime):
        # SQLite 的 func.now() 返回 UTC 时间但没有时区信息
        # 确保返回带时区的 ISO 格式，前端才能正确转换为本地时间
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    
    @field_serializer("raw_data_expires_at")
    def serialize_expires_at(self, dt: Optional[datetime]):
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()


class LogsPage(BaseModel):
    items: List[LogEntry]
    total: int
    page: int
    page_size: int
    total_pages: int


# 可手动清理的日志字段（大字段优先）
LOG_CLEARABLE_FIELDS: Dict[str, str] = {
    "request_headers": "用户请求头(request_headers)",
    "request_body": "用户请求体(request_body)",
    "upstream_request_headers": "上游请求头(upstream_request_headers)",
    "upstream_request_body": "上游请求体(upstream_request_body)",
    # 修改原因：新增上游响应头后，日志清理接口也需要允许清空该字段。
    # 修改方式：把 upstream_response_headers 加入后端清理白名单。
    # 目的：保持 Settings.tsx 可选字段与后端可清理字段一致。
    "upstream_response_headers": "上游响应头(upstream_response_headers)",
    "upstream_response_body": "上游响应体(upstream_response_body)",
    "response_body": "返回给用户的响应体(response_body)",
    "retry_path": "重试路径(retry_path)",
    "text": "文本摘要(text)",
}

DEFAULT_LOG_CLEANUP_FIELDS: List[str] = [
    "request_headers",
    "request_body",
    "upstream_request_headers",
    "upstream_request_body",
    # 修改原因：默认清理原始日志数据时也应覆盖新增的上游响应头字段。
    # 修改方式：将 upstream_response_headers 加入 DEFAULT_LOG_CLEANUP_FIELDS。
    # 目的：避免自动默认选择遗漏该字段导致旧响应头长期保留。
    "upstream_response_headers",
    "upstream_response_body",
    "response_body",
    "retry_path",
]


class LogsCleanupRequest(BaseModel):
    # dry_run=true 时仅预览，不执行写操作
    dry_run: bool = True

    # clear_fields: 清空指定字段内容但保留日志行
    # delete_rows:   直接删除匹配日志行
    action: Literal["clear_fields", "delete_rows"] = "clear_fields"

    # 仅在 action=clear_fields 时使用
    fields: List[str] = Field(default_factory=lambda: DEFAULT_LOG_CLEANUP_FIELDS.copy())

    # 时间范围过滤：
    # - older_than_hours 与 start_time/end_time 互斥
    older_than_hours: Optional[int] = Field(default=None, ge=1, le=24 * 3650)
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    # 其他维度过滤
    provider: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    success: Optional[bool] = None
    status_codes: Optional[List[int]] = None
    flagged_only: bool = False


class LogsCleanupResponse(BaseModel):
    dry_run: bool
    action: str
    matched_rows: int
    affected_rows: int
    selected_fields: List[str]
    non_null_counts: Dict[str, int]
    filters: Dict[str, Any]
    message: str


class KeyAnalyticsSummaryItem(BaseModel):
    key_hash: str
    api_key_prefix: str
    api_key_name: Optional[str] = None
    api_key_group: Optional[str] = None
    total_requests: int = 0
    success_count: int = 0
    success_rate: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost: float = 0.0
    unique_ips: int = 0
    unique_models: int = 0
    last_used: Optional[str] = None


class KeyAnalyticsOverview(BaseModel):
    total_requests: int = 0
    total_cost: float = 0.0
    active_keys: int = 0
    active_ips: int = 0


class KeyAnalyticsSummaryResponse(BaseModel):
    data: List[KeyAnalyticsSummaryItem]
    # 修改原因：前端摘要卡片中的活跃 Key 和活跃 IP 应统计完整时间范围，不能受表格 limit 影响。
    # 修改方式：在列表数据之外新增 summary 元数据，由后端按相同时间范围单独聚合。
    # 目的：保证概览卡片和排行表既共享筛选范围，又能分别表达全局摘要与 Top N 明细。
    summary: KeyAnalyticsOverview = Field(default_factory=KeyAnalyticsOverview)
    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None
    hours: Optional[int] = None
    limit: int = 50


class KeyAnalyticsIpDistributionItem(BaseModel):
    ip: Optional[str] = None
    request_count: int = 0
    last_used: Optional[str] = None
    blocked: bool = False


class KeyAnalyticsModelDistributionItem(BaseModel):
    model: Optional[str] = None
    request_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


class KeyAnalyticsModelTrendEntry(BaseModel):
    timestamp: str
    model: str
    request_count: int = 0


class KeyAnalyticsRecentErrorItem(BaseModel):
    timestamp: Optional[str] = None
    model: Optional[str] = None
    status_code: Optional[int] = None
    provider: Optional[str] = None


class KeyAnalyticsDetailResponse(BaseModel):
    key_hash: str
    api_key_prefix: str
    api_key_name: Optional[str] = None
    api_key_group: Optional[str] = None
    ip_distribution: List[KeyAnalyticsIpDistributionItem]
    model_distribution: List[KeyAnalyticsModelDistributionItem]
    model_trend: List[KeyAnalyticsModelTrendEntry]
    model_trend_models: List[str] = []
    recent_errors: List[KeyAnalyticsRecentErrorItem]
    granularity: Literal["hour", "day"]
    start_datetime: Optional[str] = None
    end_datetime: Optional[str] = None


# 修改原因：日志列表接口不能再 SELECT *，否则会把请求体、响应体和头信息等大 TEXT 字段全部读入内存。
# 修改方式：用 ORM 表结构生成完整列集合，再显式排除只应在详情页读取的原始数据字段。
# 目的：保证新增列默认会进入列表字段，而高成本原始字段始终只由 /v1/logs/{id} 单条详情接口读取。
LOG_LIST_EXCLUDED_FIELD_NAMES = (
    "request_headers",
    "request_body",
    "upstream_request_headers",
    "upstream_request_body",
    "upstream_response_headers",
    "upstream_response_body",
    "response_body",
)
LOG_DETAIL_FIELD_NAMES = tuple(column.key for column in RequestStat.__table__.columns)
LOG_LIST_COLUMN_NAMES = tuple(
    column_name
    for column_name in LOG_DETAIL_FIELD_NAMES
    if column_name not in LOG_LIST_EXCLUDED_FIELD_NAMES
)
LOG_LIST_SQL_COLUMN_CLAUSE = ", ".join(LOG_LIST_COLUMN_NAMES)


# ============ Helper Functions ============


def _normalize_cleanup_fields(fields: List[str]) -> List[str]:
    seen = set()
    normalized: List[str] = []
    for item in fields or []:
        key = str(item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    return normalized


def parse_datetime_input(dt_input: str) -> datetime:
    """解析 ISO 8601 字符串或 Unix 时间戳"""
    try:
        return datetime.fromtimestamp(float(dt_input), tz=timezone.utc)
    except ValueError:
        try:
            if dt_input.endswith('Z'):
                dt_input = dt_input[:-1] + '+00:00'
            dt_obj = datetime.fromisoformat(dt_input)
            if dt_obj.tzinfo is None:
                dt_obj = dt_obj.replace(tzinfo=timezone.utc)
            return dt_obj.astimezone(timezone.utc)
        except ValueError:
            raise ValueError(
                f"Invalid datetime format: {dt_input}. "
                "Use ISO 8601 (YYYY-MM-DDTHH:MM:SSZ) or Unix timestamp."
            )


# 修改原因：D1 分支使用手写 SQL，过去 SELECT * 和独立 COUNT 会重复扫描并读取大字段。
# 修改方式：把轻量列清单拼成显式 SELECT，并在同一个查询中用窗口函数返回 total。
# 目的：让测试和运行时代码共用同一个 SQL 构造入口，避免列表接口退回 SELECT *。
def _build_d1_logs_list_sql() -> str:
    return f"SELECT {LOG_LIST_SQL_COLUMN_CLAUSE} FROM request_stats WHERE 1=1"


# 修改原因：SQLite/PostgreSQL/MySQL 分支同样需要显式列，不能通过 ORM 实体隐式 SELECT *。
# 修改方式：按字段名生成 SQLAlchemy 列对象，列表查询使用轻量列，详情查询使用完整列。
# 目的：保持 D1 和 SQLAlchemy 两条数据库路径的日志字段策略一致。
def _log_list_sa_columns() -> List[Any]:
    return [getattr(RequestStat, column_name) for column_name in LOG_LIST_COLUMN_NAMES]


def _log_detail_sa_columns() -> List[Any]:
    return [getattr(RequestStat, column_name) for column_name in LOG_DETAIL_FIELD_NAMES]


# 修改原因：列表查询和详情查询现在分别返回字典式行数据，需要统一转成 LogEntry。
# 修改方式：集中处理时间解析、API key 掩码、过期原始数据隐藏以及数值类型转换。
# 目的：减少 D1 与 SQLAlchemy 分支重复逻辑，并确保列表不返回大字段、详情才返回完整原始字段。
def _to_optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_api_key_prefix(raw_api_key: str) -> str:
    if raw_api_key and len(raw_api_key) > 11:
        return f"{raw_api_key[:7]}...{raw_api_key[-4:]}"
    return raw_api_key


def _log_raw_field(row: Mapping[str, Any], field_name: str, *, include_raw_fields: bool, raw_data_expired: bool) -> Optional[str]:
    if not include_raw_fields or raw_data_expired:
        return None
    value = row.get(field_name)
    return str(value) if value is not None else None


def _log_entry_from_mapping(
    row: Mapping[str, Any],
    *,
    include_raw_fields: bool,
    now: Optional[datetime] = None,
) -> LogEntry:
    now = now or datetime.now(timezone.utc)
    timestamp = parse_d1_datetime(row.get("timestamp")) or now
    raw_expires_at = parse_d1_datetime(row.get("raw_data_expires_at"))
    raw_data_expired = raw_expires_at is not None and raw_expires_at < now
    raw_api_key = row.get("api_key") or ""

    return LogEntry(
        id=int(row.get("id") or 0),
        timestamp=timestamp,
        endpoint=row.get("endpoint"),
        client_ip=row.get("client_ip"),
        provider=row.get("provider"),
        model=row.get("model"),
        api_key_prefix=_build_api_key_prefix(str(raw_api_key)),
        process_time=_to_optional_float(row.get("process_time")),
        first_response_time=_to_optional_float(row.get("first_response_time")),
        prompt_tokens=int(row.get("prompt_tokens") or 0),
        completion_tokens=int(row.get("completion_tokens") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
        cached_tokens=int(row.get("cached_tokens") or 0),
        cache_creation_tokens=int(row.get("cache_creation_tokens") or 0),
        success=_bool_from_db(row.get("success")),
        status_code=_to_optional_int(row.get("status_code")),
        prompt_price=_to_optional_float(row.get("prompt_price")),
        completion_price=_to_optional_float(row.get("completion_price")),
        is_flagged=_bool_from_db(row.get("is_flagged")),
        provider_id=row.get("provider_id"),
        provider_key_index=_to_optional_int(row.get("provider_key_index")),
        api_key_name=row.get("api_key_name"),
        api_key_group=row.get("api_key_group"),
        retry_count=_to_optional_int(row.get("retry_count")),
        retry_path=row.get("retry_path") if not raw_data_expired else None,
        request_headers=_log_raw_field(row, "request_headers", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        request_body=_log_raw_field(row, "request_body", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        upstream_request_headers=_log_raw_field(row, "upstream_request_headers", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        upstream_request_body=_log_raw_field(row, "upstream_request_body", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        upstream_response_headers=_log_raw_field(row, "upstream_response_headers", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        upstream_response_body=_log_raw_field(row, "upstream_response_body", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        response_body=_log_raw_field(row, "response_body", include_raw_fields=include_raw_fields, raw_data_expired=raw_data_expired),
        raw_data_expires_at=raw_expires_at,
    )


def _build_key_analytics_time_range(
    hours: Optional[int],
    start_datetime: Optional[str],
    end_datetime: Optional[str],
) -> tuple[datetime, datetime, Optional[int]]:
    """解析 Key Analytics 时间范围。

    修改原因：两个 Key Analytics 端点共享同一套 hours/start/end 参数，重复解析容易出现边界不一致。
    修改方式：集中处理默认 24 小时、ISO/时间戳解析和起止时间校验。
    目的：保证汇总与详情下钻使用完全一致的时间过滤范围。
    """

    now = datetime.now(timezone.utc)
    effective_hours = hours if hours is not None else 24

    try:
        if start_datetime or end_datetime:
            start_dt = parse_datetime_input(start_datetime) if start_datetime else now - timedelta(hours=effective_hours)
            end_dt = parse_datetime_input(end_datetime) if end_datetime else now
        else:
            start_dt = now - timedelta(hours=effective_hours)
            end_dt = now
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if end_dt < start_dt:
        raise HTTPException(status_code=400, detail="end_datetime cannot be before start_datetime.")

    return start_dt, end_dt, effective_hours


def _safe_int(value: Any) -> int:
    """把数据库聚合值安全转成 int。

    修改原因：SQLite、D1 和不同驱动可能把 COUNT/SUM 返回为 int、float、Decimal 或字符串。
    修改方式：统一空值归零，再尝试 int(float(value))。
    目的：让接口返回类型稳定，不受数据库驱动影响。
    """

    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _safe_float(value: Any) -> float:
    """把数据库聚合值安全转成 float。

    修改原因：费用聚合值在不同数据库驱动中可能不是 Python float。
    修改方式：统一空值归零并捕获类型转换错误。
    目的：让费用字段始终以数值形式返回给前端图表。
    """

    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _key_analytics_hash(api_key: str) -> str:
    """生成前端使用的 Key 标识。

    修改原因：前端不能接触完整 API Key，但需要稳定标识某个 Key 用于下钻。
    修改方式：仅对 request_stats 中保存的 api_key 字段计算 SHA-256，并截取前 16 位。
    目的：避免暴露原始 Key，同时让列表和详情可以通过哈希关联。
    """

    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def _key_analytics_prefix(api_key: str) -> str:
    """生成 Key Analytics 展示前缀。

    修改原因：request_stats.api_key 可能是原始 Key，也可能已经是前缀加星号的脱敏值，接口必须统一保护输出。
    修改方式：只保留前 8 位并追加 ***；若数据库里已经包含星号，也不会尝试还原。
    目的：确保新页面不暴露完整 API Key。
    """

    text = str(api_key or "")
    if not text:
        return ""
    if len(text) <= 8:
        return f"{text}***"
    return f"{text[:8]}***"


def _stringify_datetime(value: Any) -> Optional[str]:
    """把数据库时间值转换为 ISO 字符串或原始字符串。

    修改原因：D1 返回字符串，SQLAlchemy 可能返回 datetime，前端只需要稳定可解析的时间文本。
    修改方式：优先用 parse_d1_datetime 归一到 UTC ISO，无法解析时返回原字符串。
    目的：降低跨数据库时间格式差异对页面展示的影响。
    """

    if value is None:
        return None
    parsed = parse_d1_datetime(value)
    if parsed is not None:
        return parsed.isoformat()
    return str(value)


def _key_analytics_summary_from_row(row: Mapping[str, Any]) -> Dict[str, Any]:
    """把数据库汇总行转换为前端安全结构。

    修改原因：聚合查询需要隐藏 api_key 原值，同时补齐成功率、费用和数值类型转换。
    修改方式：从行中取 api_key 计算 hash/prefix，然后移除原始值并构造响应字典。
    目的：让 D1 和 SQLAlchemy 分支共用一套输出规则。
    """

    api_key = str(row.get("api_key") or "")
    total_requests = _safe_int(row.get("total_requests"))
    success_count = _safe_int(row.get("success_count"))
    success_rate = round((success_count / total_requests) * 100, 2) if total_requests > 0 else 0.0

    return {
        "key_hash": _key_analytics_hash(api_key),
        "api_key_prefix": _key_analytics_prefix(api_key),
        "api_key_name": row.get("api_key_name"),
        "api_key_group": row.get("api_key_group"),
        "total_requests": total_requests,
        "success_count": success_count,
        "success_rate": success_rate,
        "total_prompt_tokens": _safe_int(row.get("total_prompt_tokens")),
        "total_completion_tokens": _safe_int(row.get("total_completion_tokens")),
        "total_cost": round(_safe_float(row.get("total_cost")), 8),
        "unique_ips": _safe_int(row.get("unique_ips")),
        "unique_models": _safe_int(row.get("unique_models")),
        "last_used": _stringify_datetime(row.get("last_used")),
    }


def _key_analytics_cost_sql() -> str:
    """返回跨 SQLite/D1 可用的费用表达式。

    修改原因：Key Analytics 多个查询都会使用同一费用公式，手写多处容易出现字段或除数不一致。
    修改方式：集中生成 COALESCE 保护后的 SQL 表达式。
    目的：保证总费用、模型分布和趋势费用的计算口径一致。
    """

    return "(COALESCE(prompt_tokens, 0) * COALESCE(prompt_price, 0.0) + COALESCE(completion_tokens, 0) * COALESCE(completion_price, 0.0)) / 1000000.0"


def _key_analytics_sa_cost_expr():
    """返回 SQLAlchemy 费用表达式。

    修改原因：SQLAlchemy 分支不能复用手写 SQL 字符串。
    修改方式：使用 func.coalesce 构建与 D1 分支一致的表达式。
    目的：保持跨数据库统计口径一致。
    """

    return (
        func.coalesce(RequestStat.prompt_tokens, 0) * func.coalesce(RequestStat.prompt_price, 0.0)
        + func.coalesce(RequestStat.completion_tokens, 0) * func.coalesce(RequestStat.completion_price, 0.0)
    ) / 1000000.0


def _build_cleanup_time_filters(payload: LogsCleanupRequest) -> tuple[Optional[datetime], Optional[datetime], Optional[datetime], Dict[str, Any]]:
    """解析并返回清理任务的时间过滤条件。

    返回值：
    - cutoff_dt:  older_than_hours 对应的截止时间（timestamp < cutoff_dt）
    - start_dt:   起始时间（timestamp >= start_dt）
    - end_dt:     结束时间（timestamp <= end_dt）
    - filters:    可回传给前端的过滤摘要
    """

    if payload.older_than_hours is not None and (payload.start_time or payload.end_time):
        raise HTTPException(
            status_code=400,
            detail="older_than_hours cannot be used together with start_time/end_time.",
        )

    filters: Dict[str, Any] = {}

    cutoff_dt: Optional[datetime] = None
    if payload.older_than_hours is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=payload.older_than_hours)
        filters["older_than_hours"] = payload.older_than_hours
        filters["older_than_before"] = cutoff_dt.isoformat()

    start_dt: Optional[datetime] = None
    if payload.start_time:
        try:
            start_dt = parse_datetime_input(payload.start_time)
            filters["start_time"] = start_dt.isoformat()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid start_time: {e}") from e

    end_dt: Optional[datetime] = None
    if payload.end_time:
        try:
            end_dt = parse_datetime_input(payload.end_time)
            filters["end_time"] = end_dt.isoformat()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid end_time: {e}") from e

    if start_dt and end_dt and end_dt < start_dt:
        raise HTTPException(status_code=400, detail="end_time must be greater than or equal to start_time.")

    return cutoff_dt, start_dt, end_dt, filters


def _validate_cleanup_request(payload: LogsCleanupRequest) -> tuple[str, List[str]]:
    """校验清理请求参数，返回 action 与规范化后的字段列表。"""

    action = (payload.action or "").strip().lower()
    if action not in {"clear_fields", "delete_rows"}:
        raise HTTPException(status_code=400, detail="Invalid action. Allowed: clear_fields, delete_rows.")

    selected_fields = _normalize_cleanup_fields(payload.fields)
    invalid_fields = [field for field in selected_fields if field not in LOG_CLEARABLE_FIELDS]
    if invalid_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid fields: {invalid_fields}. Allowed fields: {list(LOG_CLEARABLE_FIELDS.keys())}",
        )

    if action == "clear_fields" and not selected_fields:
        raise HTTPException(status_code=400, detail="fields is required when action=clear_fields.")

    if payload.status_codes:
        invalid_codes = [code for code in payload.status_codes if (code < 100 or code > 599)]
        if invalid_codes:
            raise HTTPException(status_code=400, detail=f"Invalid status_codes: {invalid_codes}")

    return action, selected_fields


# ============ Routes ============

@router.get("/v1/stats", dependencies=[Depends(rate_limit_dependency)])
async def get_stats(
    request: Request,
    token: str = Depends(verify_admin_api_key),
    hours: int = Query(default=24, ge=1, le=720, description="Number of hours to look back for stats (1-720)")
):
    """
    ## 获取统计数据

    使用 `/v1/stats` 获取最近 24 小时各个渠道的使用情况统计。同时带上自己 Zoaholic 实例的 admin API key。

    数据包括：

    1. 每个渠道下面每个模型的成功率，成功率从高到低排序。
    2. 每个渠道总的成功率，成功率从高到低排序。
    3. 每个模型在所有渠道总的请求次数。
    4. 每个端点的请求次数。
    5. 每个ip请求的次数。

    `/v1/stats?hours=48` 参数 `hours` 可以控制返回最近多少小时的数据统计，不传 `hours` 这个参数，默认统计最近 24 小时的统计数据。
    """
    if DISABLE_DATABASE:
        return JSONResponse(content={"stats": {}})
    
    start_time = datetime.now(timezone.utc) - timedelta(hours=hours)

    total_cost = 0.0
    if (DB_TYPE or "sqlite").lower() == "d1":
        from db import d1_client
        if d1_client is None:
            return JSONResponse(content={"stats": {}})

        channel_model_rows = await d1_client.query_all(
            "SELECT provider, model, COUNT(*) AS total, "
            "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count "
            "FROM channel_stats WHERE timestamp >= ? GROUP BY provider, model",
            [start_time],
        )
        channel_rows = await d1_client.query_all(
            "SELECT provider, COUNT(*) AS total, "
            "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count "
            "FROM channel_stats WHERE timestamp >= ? GROUP BY provider",
            [start_time],
        )
        model_rows = await d1_client.query_all(
            "SELECT model, COUNT(*) AS count FROM channel_stats "
            "WHERE timestamp >= ? GROUP BY model ORDER BY count DESC",
            [start_time],
        )
        endpoint_rows = await d1_client.query_all(
            "SELECT endpoint, COUNT(*) AS count FROM request_stats "
            "WHERE timestamp >= ? GROUP BY endpoint ORDER BY count DESC",
            [start_time],
        )
        ip_rows = await d1_client.query_all(
            "SELECT client_ip, COUNT(*) AS count FROM request_stats "
            "WHERE timestamp >= ? GROUP BY client_ip ORDER BY count DESC",
            [start_time],
        )

        channel_model_stats = [
            {
                "provider": row.get("provider"),
                "model": row.get("model"),
                "total": int(row.get("total") or 0),
                "success_count": int(row.get("success_count") or 0),
            }
            for row in channel_model_rows
        ]
        channel_stats = [
            {
                "provider": row.get("provider"),
                "total": int(row.get("total") or 0),
                "success_count": int(row.get("success_count") or 0),
            }
            for row in channel_rows
        ]
        model_stats = [
            {"model": row.get("model"), "count": int(row.get("count") or 0)}
            for row in model_rows
        ]
        endpoint_stats = [
            {"endpoint": row.get("endpoint"), "count": int(row.get("count") or 0)}
            for row in endpoint_rows
        ]
        ip_stats = [
            {"client_ip": row.get("client_ip"), "count": int(row.get("count") or 0)}
            for row in ip_rows
        ]
    else:
        async with async_session_scope() as session:
            # 1. 每个渠道下面每个模型的成功率
            channel_model_stats_rs = await session.execute(
                select(
                    ChannelStat.provider,
                    ChannelStat.model,
                    func.count().label('total'),
                    func.sum(case((ChannelStat.success, 1), else_=0)).label('success_count')
                )
                .where(ChannelStat.timestamp >= start_time)
                .group_by(ChannelStat.provider, ChannelStat.model)
            )
            channel_model_stats = [
                {
                    "provider": stat.provider,
                    "model": stat.model,
                    "total": int(stat.total or 0),
                    "success_count": int(stat.success_count or 0),
                }
                for stat in channel_model_stats_rs.fetchall()
            ]

            # 2. 每个渠道总的成功率
            channel_stats_rs = await session.execute(
                select(
                    ChannelStat.provider,
                    func.count().label('total'),
                    func.sum(case((ChannelStat.success, 1), else_=0)).label('success_count')
                )
                .where(ChannelStat.timestamp >= start_time)
                .group_by(ChannelStat.provider)
            )
            channel_stats = [
                {
                    "provider": stat.provider,
                    "total": int(stat.total or 0),
                    "success_count": int(stat.success_count or 0),
                }
                for stat in channel_stats_rs.fetchall()
            ]

            # 3. 每个模型在所有渠道总的请求次数
            model_stats_rs = await session.execute(
                select(ChannelStat.model, func.count().label('count'))
                .where(ChannelStat.timestamp >= start_time)
                .group_by(ChannelStat.model)
                .order_by(desc('count'))
            )
            model_stats = [{"model": stat.model, "count": int(stat.count or 0)} for stat in model_stats_rs.fetchall()]

            # 4. 每个端点的请求次数
            endpoint_stats_rs = await session.execute(
                select(RequestStat.endpoint, func.count().label('count'))
                .where(RequestStat.timestamp >= start_time)
                .group_by(RequestStat.endpoint)
                .order_by(desc('count'))
            )
            endpoint_stats = [
                {"endpoint": stat.endpoint, "count": int(stat.count or 0)}
                for stat in endpoint_stats_rs.fetchall()
            ]

            # 5. 每个ip请求的次数
            ip_stats_rs = await session.execute(
                select(RequestStat.client_ip, func.count().label('count'))
                .where(RequestStat.timestamp >= start_time)
                .group_by(RequestStat.client_ip)
                .order_by(desc('count'))
            )
            ip_stats = [{"client_ip": stat.client_ip, "count": int(stat.count or 0)} for stat in ip_stats_rs.fetchall()]
    # 计算选定时间范围内的总费用
    try:
        total_cost = await compute_total_cost_from_db(start_dt_obj=start_time)
    except Exception:
        total_cost = 0.0


    stats = {
        "time_range": f"Last {hours} hours",
        "channel_model_success_rates": [
            {
                "provider": stat.get("provider"),
                "model": stat.get("model"),
                "success_rate": (stat.get("success_count", 0) / stat.get("total", 0)) if stat.get("total", 0) > 0 else 0,
                "total_requests": stat.get("total", 0)
            } for stat in sorted(channel_model_stats, key=lambda x: (x.get("success_count", 0) / x.get("total", 0)) if x.get("total", 0) > 0 else 0, reverse=True)
        ],
        "channel_success_rates": [
            {
                "provider": stat.get("provider"),
                "success_rate": (stat.get("success_count", 0) / stat.get("total", 0)) if stat.get("total", 0) > 0 else 0,
                "total_requests": stat.get("total", 0)
            } for stat in sorted(channel_stats, key=lambda x: (x.get("success_count", 0) / x.get("total", 0)) if x.get("total", 0) > 0 else 0, reverse=True)
        ],
        "model_request_counts": [
            {
                "model": stat.get("model"),
                "count": stat.get("count", 0)
            } for stat in model_stats
        ],
        "endpoint_request_counts": [
            {
                "endpoint": stat.get("endpoint"),
                "count": stat.get("count", 0)
            } for stat in endpoint_stats
        ],
        "ip_request_counts": [
            {
                "ip": stat.get("client_ip"),
                "count": stat.get("count", 0)
            } for stat in ip_stats
        ],
        "total_cost": round(total_cost, 6),
    }

    return JSONResponse(content=stats)


# ============ Key Analytics (API Key 用量分析) ============

@router.get(
    "/v1/stats/key_analytics/summary",
    response_model=KeyAnalyticsSummaryResponse,
    dependencies=[Depends(rate_limit_dependency)],
)
async def get_key_analytics_summary(
    request: Request = None,
    token: str = Depends(verify_admin_api_key),
    hours: Optional[int] = Query(default=24, ge=1, le=8760),
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
):
    """按 API Key 聚合请求用量。

    修改原因：管理端需要独立查看每个用户 API Key 的请求量、成功率、费用和活跃来源。
    修改方式：直接从 request_stats 按 api_key 分组聚合，并只返回 hash 与前缀，不返回数据库中的 api_key 原值。
    目的：支撑前端 Key Analytics 页面，同时避免完整 API Key 暴露到浏览器。
    """

    if DISABLE_DATABASE:
        return KeyAnalyticsSummaryResponse(data=[], hours=hours or 24, limit=limit)

    start_dt, end_dt, effective_hours = _build_key_analytics_time_range(hours, start_datetime, end_datetime)
    db_type = (DB_TYPE or "sqlite").lower()

    summary_meta = KeyAnalyticsOverview()

    if db_type == "d1":
        from db import d1_client
        if d1_client is None:
            return KeyAnalyticsSummaryResponse(data=[], summary=summary_meta, hours=effective_hours, limit=limit)

        cost_sql = _key_analytics_cost_sql()
        summary_rows = await d1_client.query_all(
            "SELECT COUNT(*) AS total_requests, "
            f"COALESCE(SUM({_key_analytics_cost_sql()}), 0.0) AS total_cost, "
            "COUNT(DISTINCT api_key) AS active_keys, "
            "COUNT(DISTINCT client_ip) AS active_ips "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? "
            "AND api_key IS NOT NULL AND api_key != ''",
            [format_d1_datetime(start_dt), format_d1_datetime(end_dt)],
        )
        summary_row = summary_rows[0] if summary_rows else {}
        summary_meta = KeyAnalyticsOverview(
            total_requests=_safe_int(summary_row.get("total_requests")),
            total_cost=round(_safe_float(summary_row.get("total_cost")), 8),
            active_keys=_safe_int(summary_row.get("active_keys")),
            active_ips=_safe_int(summary_row.get("active_ips")),
        )
        sql = (
            "SELECT api_key, MAX(api_key_name) AS api_key_name, MAX(api_key_group) AS api_key_group, "
            "COUNT(*) AS total_requests, "
            "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS success_count, "
            "COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens, "
            f"COALESCE(SUM({cost_sql}), 0.0) AS total_cost, "
            "COUNT(DISTINCT client_ip) AS unique_ips, "
            "COUNT(DISTINCT model) AS unique_models, "
            "MAX(timestamp) AS last_used "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? "
            "AND api_key IS NOT NULL AND api_key != '' "
            "GROUP BY api_key ORDER BY total_requests DESC LIMIT ?"
        )
        rows = await d1_client.query_all(sql, [format_d1_datetime(start_dt), format_d1_datetime(end_dt), limit])
    else:
        async with async_session_scope() as session:
            cost_expr = _key_analytics_sa_cost_expr()
            summary_query = (
                select(
                    func.count(RequestStat.id).label("total_requests"),
                    func.coalesce(func.sum(cost_expr), 0.0).label("total_cost"),
                    func.count(func.distinct(RequestStat.api_key)).label("active_keys"),
                    func.count(func.distinct(RequestStat.client_ip)).label("active_ips"),
                )
                .where(
                    RequestStat.timestamp >= start_dt,
                    RequestStat.timestamp <= end_dt,
                    RequestStat.api_key.isnot(None),
                    RequestStat.api_key != "",
                )
            )
            summary_result = await session.execute(summary_query)
            summary_row = summary_result.mappings().one_or_none() or {}
            summary_meta = KeyAnalyticsOverview(
                total_requests=_safe_int(summary_row.get("total_requests")),
                total_cost=round(_safe_float(summary_row.get("total_cost")), 8),
                active_keys=_safe_int(summary_row.get("active_keys")),
                active_ips=_safe_int(summary_row.get("active_ips")),
            )
            query = (
                select(
                    RequestStat.api_key.label("api_key"),
                    func.max(RequestStat.api_key_name).label("api_key_name"),
                    func.max(RequestStat.api_key_group).label("api_key_group"),
                    func.count(RequestStat.id).label("total_requests"),
                    func.sum(case((RequestStat.status_code < 400, 1), else_=0)).label("success_count"),
                    func.coalesce(func.sum(RequestStat.prompt_tokens), 0).label("total_prompt_tokens"),
                    func.coalesce(func.sum(RequestStat.completion_tokens), 0).label("total_completion_tokens"),
                    func.coalesce(func.sum(cost_expr), 0.0).label("total_cost"),
                    func.count(func.distinct(RequestStat.client_ip)).label("unique_ips"),
                    func.count(func.distinct(RequestStat.model)).label("unique_models"),
                    func.max(RequestStat.timestamp).label("last_used"),
                )
                .where(
                    RequestStat.timestamp >= start_dt,
                    RequestStat.timestamp <= end_dt,
                    RequestStat.api_key.isnot(None),
                    RequestStat.api_key != "",
                )
                .group_by(RequestStat.api_key)
                .order_by(desc("total_requests"))
                .limit(limit)
            )
            result = await session.execute(query)
            rows = result.mappings().all()

    data = [_key_analytics_summary_from_row(row) for row in rows]
    return KeyAnalyticsSummaryResponse(
        data=[KeyAnalyticsSummaryItem(**item) for item in data],
        summary=summary_meta,
        start_datetime=start_dt.isoformat(),
        end_datetime=end_dt.isoformat(),
        hours=effective_hours,
        limit=limit,
    )


@router.get(
    "/v1/stats/key_analytics/{key_hash}",
    response_model=KeyAnalyticsDetailResponse,
    dependencies=[Depends(rate_limit_dependency)],
)
async def get_key_analytics_detail(
    key_hash: str,
    request: Request = None,
    token: str = Depends(verify_admin_api_key),
    hours: Optional[int] = Query(default=24, ge=1, le=8760),
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
    granularity: Literal["hour", "day"] = Query(default="hour"),
):
    """获取单个 API Key 的下钻分析。

    修改原因：前端下钻只能传 key_hash，不能传完整或脱敏 api_key 原值。
    修改方式：先在时间范围内枚举 request_stats.api_key 并计算哈希，命中后再用该 api_key 精确过滤详情数据。
    目的：既保护 Key 原值，又让详情查询保持索引友好的精确匹配。
    """

    if DISABLE_DATABASE:
        raise HTTPException(status_code=503, detail="Database is disabled.")

    normalized_hash = (key_hash or "").strip().lower()
    if not normalized_hash:
        raise HTTPException(status_code=400, detail="key_hash is required.")

    start_dt, end_dt, _effective_hours = _build_key_analytics_time_range(hours, start_datetime, end_datetime)
    db_type = (DB_TYPE or "sqlite").lower()
    cost_sql = _key_analytics_cost_sql()

    matched_api_key: Optional[str] = None
    matched_name: Optional[str] = None
    matched_group: Optional[str] = None

    if db_type == "d1":
        from db import d1_client
        if d1_client is None:
            raise HTTPException(status_code=503, detail="D1 client is not initialized.")

        base_params = [format_d1_datetime(start_dt), format_d1_datetime(end_dt)]
        candidate_rows = await d1_client.query_all(
            "SELECT api_key, MAX(api_key_name) AS api_key_name, MAX(api_key_group) AS api_key_group "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? "
            "AND api_key IS NOT NULL AND api_key != '' GROUP BY api_key",
            base_params,
        )
        for row in candidate_rows:
            api_key = str(row.get("api_key") or "")
            if _key_analytics_hash(api_key) == normalized_hash:
                matched_api_key = api_key
                matched_name = row.get("api_key_name")
                matched_group = row.get("api_key_group")
                break

        if matched_api_key is None:
            raise HTTPException(status_code=404, detail="API key analytics target not found in the selected time range.")

        detail_params = [*base_params, matched_api_key]
        ip_rows = await d1_client.query_all(
            "SELECT client_ip AS ip, COUNT(*) AS request_count, MAX(timestamp) AS last_used "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? AND api_key = ? "
            "GROUP BY client_ip ORDER BY request_count DESC LIMIT ?",
            [*detail_params, limit],
        )
        model_rows = await d1_client.query_all(
            "SELECT model, COUNT(*) AS request_count, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            f"COALESCE(SUM({cost_sql}), 0.0) AS cost "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? AND api_key = ? "
            "GROUP BY model ORDER BY request_count DESC LIMIT ?",
            [*detail_params, limit],
        )
        time_group = "strftime('%Y-%m-%d 00:00:00', timestamp)" if granularity == "day" else "strftime('%Y-%m-%d %H:00:00', timestamp)"
        trend_rows = await d1_client.query_all(
            f"SELECT {time_group} AS time_bucket, COUNT(*) AS request_count, "
            "SUM(CASE WHEN status_code < 400 THEN 1 ELSE 0 END) AS success_count, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            f"COALESCE(SUM({cost_sql}), 0.0) AS cost "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? AND api_key = ? "
            "GROUP BY time_bucket ORDER BY time_bucket ASC",
            detail_params,
        )
        error_rows = await d1_client.query_all(
            "SELECT timestamp, model, status_code, provider "
            "FROM request_stats WHERE timestamp >= ? AND timestamp <= ? AND api_key = ? "
            "AND status_code >= 400 ORDER BY timestamp DESC LIMIT 20",
            detail_params,
        )
    else:
        async with async_session_scope() as session:
            candidate_query = (
                select(
                    RequestStat.api_key.label("api_key"),
                    func.max(RequestStat.api_key_name).label("api_key_name"),
                    func.max(RequestStat.api_key_group).label("api_key_group"),
                )
                .where(
                    RequestStat.timestamp >= start_dt,
                    RequestStat.timestamp <= end_dt,
                    RequestStat.api_key.isnot(None),
                    RequestStat.api_key != "",
                )
                .group_by(RequestStat.api_key)
            )
            candidate_result = await session.execute(candidate_query)
            for row in candidate_result.mappings().all():
                api_key = str(row.get("api_key") or "")
                if _key_analytics_hash(api_key) == normalized_hash:
                    matched_api_key = api_key
                    matched_name = row.get("api_key_name")
                    matched_group = row.get("api_key_group")
                    break

            if matched_api_key is None:
                raise HTTPException(status_code=404, detail="API key analytics target not found in the selected time range.")

            filters = (
                RequestStat.timestamp >= start_dt,
                RequestStat.timestamp <= end_dt,
                RequestStat.api_key == matched_api_key,
            )
            cost_expr = _key_analytics_sa_cost_expr()

            ip_result = await session.execute(
                select(
                    RequestStat.client_ip.label("ip"),
                    func.count(RequestStat.id).label("request_count"),
                    func.max(RequestStat.timestamp).label("last_used"),
                )
                .where(*filters)
                .group_by(RequestStat.client_ip)
                .order_by(desc("request_count"))
                .limit(limit)
            )
            ip_rows = ip_result.mappings().all()

            model_result = await session.execute(
                select(
                    RequestStat.model.label("model"),
                    func.count(RequestStat.id).label("request_count"),
                    func.coalesce(func.sum(RequestStat.prompt_tokens), 0).label("prompt_tokens"),
                    func.coalesce(func.sum(RequestStat.completion_tokens), 0).label("completion_tokens"),
                    func.coalesce(func.sum(cost_expr), 0.0).label("cost"),
                )
                .where(*filters)
                .group_by(RequestStat.model)
                .order_by(desc("request_count"))
                .limit(limit)
            )
            model_rows = model_result.mappings().all()

            if db_type == "postgres":
                time_group = func.date_trunc(granularity, RequestStat.timestamp)
            elif db_type == "mysql":
                fmt = "%Y-%m-%d 00:00:00" if granularity == "day" else "%Y-%m-%d %H:00:00"
                time_group = func.date_format(RequestStat.timestamp, fmt)
            else:
                fmt = "%Y-%m-%d 00:00:00" if granularity == "day" else "%Y-%m-%d %H:00:00"
                time_group = func.strftime(fmt, RequestStat.timestamp)

            trend_result = await session.execute(
                select(
                    time_group.label("time_bucket"),
                    RequestStat.model.label("model"),
                    func.count(RequestStat.id).label("request_count"),
                )
                .where(*filters)
                .group_by(time_group, RequestStat.model)
                .order_by(time_group.asc() if hasattr(time_group, "asc") else time_group)
            )
            trend_rows = trend_result.mappings().all()

            error_result = await session.execute(
                select(
                    RequestStat.timestamp.label("timestamp"),
                    RequestStat.model.label("model"),
                    RequestStat.status_code.label("status_code"),
                    RequestStat.provider.label("provider"),
                )
                .where(*filters, RequestStat.status_code >= 400)
                .order_by(RequestStat.timestamp.desc())
                .limit(20)
            )
            error_rows = error_result.mappings().all()

    # IP 黑名单标注
    from core.ip_blacklist import is_ip_blacklisted
    app = request.app
    global_bl = getattr(app.state, "global_ip_blacklist", None)
    key_bls = getattr(app.state, "api_key_ip_blacklists", []) or []
    # 找到当前 key 的 index
    _api_keys = getattr(app.state, "config", {}).get("api_keys", [])
    _key_bl = None
    for _ki, _kobj in enumerate(_api_keys):
        if isinstance(_kobj, dict) and str(_kobj.get("api", "")).strip() == (matched_api_key or "").strip():
            _key_bl = key_bls[_ki] if _ki < len(key_bls) else None
            break

    def _is_ip_blocked(ip_str):
        if not ip_str:
            return False
        if is_ip_blacklisted(global_bl, ip_str):
            return True
        if _key_bl and is_ip_blacklisted(_key_bl, ip_str):
            return True
        return False

    ip_distribution = [
        KeyAnalyticsIpDistributionItem(
            ip=row.get("ip"),
            request_count=_safe_int(row.get("request_count")),
            last_used=_stringify_datetime(row.get("last_used")),
            blocked=_is_ip_blocked(row.get("ip")),
        )
        for row in ip_rows
    ]
    model_distribution = [
        KeyAnalyticsModelDistributionItem(
            model=row.get("model"),
            request_count=_safe_int(row.get("request_count")),
            prompt_tokens=_safe_int(row.get("prompt_tokens")),
            completion_tokens=_safe_int(row.get("completion_tokens")),
            cost=round(_safe_float(row.get("cost")), 8),
        )
        for row in model_rows
    ]
    # 构建按模型拆分的趋势数据
    model_trend = [
        KeyAnalyticsModelTrendEntry(
            timestamp=_stringify_datetime(row.get("time_bucket")) or str(row.get("time_bucket") or ""),
            model=row.get("model") or "unknown",
            request_count=_safe_int(row.get("request_count")),
        )
        for row in trend_rows
    ]
    # 提取趋势图中出现的模型列表（按总请求量降序，最多8个）
    _model_counts: dict = {}
    for entry in model_trend:
        _model_counts[entry.model] = _model_counts.get(entry.model, 0) + entry.request_count
    model_trend_models = sorted(_model_counts, key=lambda m: -_model_counts[m])[:8]
    recent_errors = [
        KeyAnalyticsRecentErrorItem(
            timestamp=_stringify_datetime(row.get("timestamp")),
            model=row.get("model"),
            status_code=_to_optional_int(row.get("status_code")),
            provider=row.get("provider"),
        )
        for row in error_rows
    ]

    return KeyAnalyticsDetailResponse(
        key_hash=normalized_hash,
        api_key_prefix=_key_analytics_prefix(matched_api_key or ""),
        api_key_name=matched_name,
        api_key_group=matched_group,
        ip_distribution=ip_distribution,
        model_distribution=model_distribution,
        model_trend=model_trend,
        model_trend_models=model_trend_models,
        recent_errors=recent_errors,
        granularity=granularity,
        start_datetime=start_dt.isoformat(),
        end_datetime=end_dt.isoformat(),
    )


# ============ Usage Analysis (用量分析与费用模拟) ============

class UsageAnalysisEntry(BaseModel):
    provider: str
    model: str
    request_count: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_tokens: int = 0


@router.get("/v1/stats/usage_analysis", dependencies=[Depends(rate_limit_dependency)])
async def get_usage_analysis(
    request: Request,
    token: str = Depends(verify_admin_api_key),
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    hours: Optional[int] = Query(default=24, ge=1, le=8760, description="Lookback hours (used when start/end not provided)"),
    provider: Optional[str] = Query(default=None, description="Provider filter, comma-separated for multiple"),
    model: Optional[str] = Query(default=None, description="Model filter, comma-separated for multiple"),
):
    """
    按渠道和模型分组的用量分析，返回请求次数、Token 消耗量和基于当前配置价格的实时费用。
    """
    if DISABLE_DATABASE:
        return JSONResponse(content={"data": []})

    now = datetime.now(timezone.utc)
    start_dt = None
    end_dt = None

    provider_list = [p.strip() for p in provider.split(',') if p.strip()] if provider else []
    model_list = [m.strip() for m in model.split(',') if m.strip()] if model else []

    if start_datetime or end_datetime:
        try:
            if start_datetime:
                start_dt = parse_datetime_input(start_datetime)
            if end_datetime:
                end_dt = parse_datetime_input(end_datetime)
            if start_dt and end_dt and end_dt < start_dt:
                raise HTTPException(status_code=400, detail="end_datetime cannot be before start_datetime.")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        start_dt = now - timedelta(hours=hours or 24)
        end_dt = now

    start_detail = start_dt.isoformat(timespec='seconds') if start_dt else None
    end_detail = end_dt.isoformat(timespec='seconds') if end_dt else None

    if (DB_TYPE or "sqlite").lower() == "d1":
        from db import d1_client
        if d1_client is None:
            return JSONResponse(content={"data": []})

        sql = (
            "SELECT provider, model, COUNT(*) AS request_count, "
            "COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens, "
            "COALESCE(SUM(total_tokens), 0) AS total_tokens "
            "FROM request_stats WHERE 1=1"
        )
        params: list = []
        if start_dt:
            sql += " AND timestamp >= ?"
            params.append(start_dt)
        if end_dt:
            sql += " AND timestamp <= ?"
            params.append(end_dt)
        if provider_list:
            if len(provider_list) == 1:
                sql += " AND provider = ?"
                params.append(provider_list[0])
            else:
                placeholders = ','.join(['?'] * len(provider_list))
                sql += f" AND provider IN ({placeholders})"
                params.extend(provider_list)
        if model_list:
            if len(model_list) == 1:
                sql += " AND model = ?"
                params.append(model_list[0])
            else:
                placeholders = ','.join(['?'] * len(model_list))
                sql += f" AND model IN ({placeholders})"
                params.extend(model_list)
        sql += " AND provider IS NOT NULL AND provider != ''"
        sql += " AND model IS NOT NULL AND model != ''"
        sql += " GROUP BY provider, model ORDER BY request_count DESC"

        rows = await d1_client.query_all(sql, params)
        data = [
            {
                "provider": row.get("provider", ""),
                "model": row.get("model", ""),
                "request_count": int(row.get("request_count") or 0),
                "total_prompt_tokens": int(row.get("total_prompt_tokens") or 0),
                "total_completion_tokens": int(row.get("total_completion_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
            }
            for row in rows
        ]
    else:
        async with async_session_scope() as session:
            query = select(
                RequestStat.provider,
                RequestStat.model,
                func.count().label('request_count'),
                func.coalesce(func.sum(RequestStat.prompt_tokens), 0).label('total_prompt_tokens'),
                func.coalesce(func.sum(RequestStat.completion_tokens), 0).label('total_completion_tokens'),
                func.coalesce(func.sum(RequestStat.total_tokens), 0).label('total_tokens'),
            )
            if start_dt:
                query = query.where(RequestStat.timestamp >= start_dt)
            if end_dt:
                query = query.where(RequestStat.timestamp <= end_dt)
            if provider_list:
                if len(provider_list) == 1:
                    query = query.where(RequestStat.provider == provider_list[0])
                else:
                    query = query.where(RequestStat.provider.in_(provider_list))
            if model_list:
                if len(model_list) == 1:
                    query = query.where(RequestStat.model == model_list[0])
                else:
                    query = query.where(RequestStat.model.in_(model_list))
            query = query.where(
                RequestStat.provider.isnot(None),
                RequestStat.provider != '',
                RequestStat.model.isnot(None),
                RequestStat.model != '',
            )
            query = query.group_by(RequestStat.provider, RequestStat.model)
            query = query.order_by(desc('request_count'))

            result = await session.execute(query)
            data = [
                {
                    "provider": row.provider,
                    "model": row.model,
                    "request_count": int(row.request_count or 0),
                    "total_prompt_tokens": int(row.total_prompt_tokens or 0),
                    "total_completion_tokens": int(row.total_completion_tokens or 0),
                    "total_tokens": int(row.total_tokens or 0),
                }
                for row in result.fetchall()
            ]

    # 用当前配置价格实时计算每行费用（渠道级 > 全局级 > 0）
    from core.stats import get_current_model_prices
    app = get_app()
    for entry in data:
        # 修改原因：get_current_model_prices 新增 cached_price 第三段，旧的二元组解包会在路由实时计价时报错。
        # 修改方式：这里仍只按汇总 prompt_tokens 估算当前价格成本，因此第三段用 _ 显式忽略。
        # 目的：保持该查询接口返回结构不变，同时兼容新的三元组价格接口。
        prompt_price, completion_price, _ = get_current_model_prices(
            app, entry["model"], provider_name=entry["provider"]
        )
        entry["total_cost"] = (
            entry["total_prompt_tokens"] * prompt_price
            + entry["total_completion_tokens"] * completion_price
        ) / 1_000_000

    return JSONResponse(content={
        "data": data,
        "start_datetime": start_detail,
        "end_datetime": end_detail,
        "provider_filter": provider or "all",
        "model_filter": model or "all",
    })



@router.get("/v1/stats/model_trend", dependencies=[Depends(rate_limit_dependency)])
async def get_model_trend(
    request: Request,
    token: str = Depends(verify_admin_api_key),
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    hours: Optional[int] = Query(default=24, ge=1, le=8760),
    provider: Optional[str] = None,
    model: Optional[str] = None,
    granularity: Optional[str] = Query(default=None, regex='^(hour|day)$'),
):
    """
    获取筛选模型的时间趋势数据，用于折线图展示。
    按小时聚合请求次数和 token 使用量。
    """
    if DISABLE_DATABASE:
        return JSONResponse(content={"data": []})

    now = datetime.now(timezone.utc)
    start_dt = parse_datetime_input(start_datetime) if start_datetime else (now - timedelta(hours=hours or 24))
    end_dt = parse_datetime_input(end_datetime) if end_datetime else now

    provider_list = [p.strip() for p in provider.split(',') if p.strip()] if provider else []
    model_list = [m.strip() for m in model.split(',') if m.strip()] if model else []

    # 自动选择聚合粒度：>48h 用天，否则用小时
    if not granularity:
        span_hours = (end_dt - start_dt).total_seconds() / 3600
        granularity = 'day' if span_hours > 48 else 'hour'

    if (DB_TYPE or "sqlite").lower() == "d1":
        from db import d1_client
        if granularity == 'day':
            time_group = "strftime('%Y-%m-%d', timestamp)"
        else:
            time_group = "strftime('%Y-%m-%d %H:00:00', timestamp)"
        sql = f"""
            SELECT {time_group} AS hour, model, COUNT(*) AS count,
            SUM(COALESCE(total_tokens, 0)) AS tokens,
            SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,
            SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,
            SUM(COALESCE(cached_tokens, 0)) AS cached_tokens
            FROM request_stats WHERE timestamp >= ? AND timestamp <= ?
        """
        params = [format_d1_datetime(start_dt), format_d1_datetime(end_dt)]
        if provider_list:
            sql += f" AND provider IN ({','.join(['?']*len(provider_list))})"
            params.extend(provider_list)
        if model_list:
            sql += f" AND model IN ({','.join(['?']*len(model_list))})"
            params.extend(model_list)
        sql += " AND model IS NOT NULL AND model != ''"
        sql += " GROUP BY hour, model ORDER BY hour ASC"
        
        rows = await d1_client.query_all(sql, params)
        data = rows
    else:
        async with async_session_scope() as session:
            # PostgreSQL/MySQL 等数据库使用不同的日期截断函数
            if (DB_TYPE or "").lower() == "postgres":
                time_group = func.date_trunc(granularity, RequestStat.timestamp)
                order_expr = time_group
            elif (DB_TYPE or "").lower() == "mysql":
                fmt = '%Y-%m-%d' if granularity == 'day' else '%Y-%m-%d %H:00:00'
                time_group = func.date_format(RequestStat.timestamp, fmt)
                order_expr = time_group
            else: # SQLite fallback
                fmt = '%Y-%m-%d' if granularity == 'day' else '%Y-%m-%d %H:00:00'
                time_group = func.strftime(fmt, RequestStat.timestamp)
                order_expr = time_group

            query = select(
                time_group.label('hour'),
                RequestStat.model,
                func.count().label('count'),
                func.sum(func.coalesce(RequestStat.total_tokens, 0)).label('tokens'),
                func.sum(func.coalesce(RequestStat.prompt_tokens, 0)).label('prompt_tokens'),
                func.sum(func.coalesce(RequestStat.completion_tokens, 0)).label('completion_tokens'),
                func.sum(func.coalesce(RequestStat.cached_tokens, 0)).label('cached_tokens')
            ).where(RequestStat.timestamp >= start_dt, RequestStat.timestamp <= end_dt)

            if provider_list:
                query = query.where(RequestStat.provider.in_(provider_list))
            if model_list:
                query = query.where(RequestStat.model.in_(model_list))
            query = query.where(
         RequestStat.model.isnot(None),
      RequestStat.model != ''
            )
            
            query = query.group_by(time_group, RequestStat.model).order_by(order_expr)
            result = await session.execute(query)
            data = [
                {"hour": str(row.hour), "model": row.model, "count": int(row.count), "tokens": int(row.tokens or 0),
                 "prompt_tokens": int(row.prompt_tokens or 0), "completion_tokens": int(row.completion_tokens or 0),
                 "cached_tokens": int(row.cached_tokens or 0)}
                for row in result.fetchall()
            ]

    chart_dict = {}
    tokens_chart_dict = {}
    models_seen = set()
    for item in data:
        h = item['hour']
        m = item['model']
        models_seen.add(m)
        if h not in chart_dict:
            chart_dict[h] = {"hour": h}
        if h not in tokens_chart_dict:
            tokens_chart_dict[h] = {"hour": h}
        chart_dict[h][m] = item['count']
        tokens_chart_dict[h][m] = item.get('tokens', 0) or 0

    chart_data = sorted(chart_dict.values(), key=lambda x: x['hour'])
    tokens_chart_data = sorted(tokens_chart_dict.values(), key=lambda x: x['hour'])

    # token 细分数据（按小时聚合，不按模型分）
    token_breakdown: dict = {}
    for item in data:
        h = item['hour']
        if h not in token_breakdown:
            token_breakdown[h] = {"hour": h, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        token_breakdown[h]["prompt_tokens"] += item.get("prompt_tokens", 0) or 0
        token_breakdown[h]["completion_tokens"] += item.get("completion_tokens", 0) or 0
        token_breakdown[h]["cached_tokens"] += item.get("cached_tokens", 0) or 0
    token_breakdown_data = sorted(token_breakdown.values(), key=lambda x: x['hour'])

    return JSONResponse(content={
        "data": chart_data,
        "tokens_data": tokens_chart_data,
        "token_breakdown": token_breakdown_data,
        "models": sorted(list(models_seen)),
        "granularity": granularity,
        "start_datetime": start_dt.isoformat(),
        "end_datetime": end_dt.isoformat(),
    })



@router.get("/v1/token_usage", response_model=TokenUsageResponse, dependencies=[Depends(rate_limit_dependency)])
async def get_token_usage(
    request: Request,
    api_key_param: Optional[str] = None,
    model: Optional[str] = None,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    last_n_days: Optional[int] = None,
    api_index: tuple = Depends(verify_api_key)
):
    """
    获取聚合的 token 使用统计，按 API key 和模型分组，可按时间范围过滤。
    管理员用户可以按特定 API key 过滤。
    """
    if DISABLE_DATABASE:
        raise HTTPException(status_code=503, detail="Database is disabled.")

    app = get_app()
    requesting_token = safe_get(app.state.config, 'api_keys', api_index, "api", default="")

    # 判断是否为管理员
    is_admin = False
    if hasattr(app.state, "admin_api_key") and requesting_token in app.state.admin_api_key:
        is_admin = True

    # 确定 API key 过滤器
    filter_api_key = None
    api_key_filter_detail = "all"
    if is_admin:
        if api_key_param:
            filter_api_key = api_key_param
            api_key_filter_detail = api_key_param
    else:
        filter_api_key = requesting_token
        api_key_filter_detail = "self"

    # 确定时间范围
    end_dt_obj = None
    start_dt_obj = None
    start_datetime_detail = None
    end_datetime_detail = None
    now = datetime.now(timezone.utc)

    if last_n_days is not None:
        if start_datetime or end_datetime:
            raise HTTPException(
                status_code=400,
                detail="Cannot use last_n_days with start_datetime or end_datetime."
            )
        if last_n_days <= 0:
            raise HTTPException(status_code=400, detail="last_n_days must be positive.")
        start_dt_obj = now - timedelta(days=last_n_days)
        end_dt_obj = now
        start_datetime_detail = start_dt_obj.isoformat(timespec='seconds')
        end_datetime_detail = end_dt_obj.isoformat(timespec='seconds')
    elif start_datetime or end_datetime:
        try:
            if start_datetime:
                start_dt_obj = parse_datetime_input(start_datetime)
                start_datetime_detail = start_dt_obj.isoformat(timespec='seconds')
            if end_datetime:
                end_dt_obj = parse_datetime_input(end_datetime)
                end_datetime_detail = end_dt_obj.isoformat(timespec='seconds')
            if start_dt_obj and end_dt_obj and end_dt_obj < start_dt_obj:
                raise HTTPException(
                    status_code=400,
                    detail="end_datetime cannot be before start_datetime."
                )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        start_dt_obj = now - timedelta(days=30)
        end_dt_obj = now
        start_datetime_detail = start_dt_obj.isoformat(timespec='seconds')
        end_datetime_detail = end_dt_obj.isoformat(timespec='seconds')

    usage_data = await get_usage_data(
        filter_api_key=filter_api_key,
        filter_model=model,
        start_dt_obj=start_dt_obj,
        end_dt_obj=end_dt_obj
    )

    # 获取付费 API key 状态
    if filter_api_key:
        from main import update_paid_api_keys_states
        credits, total_cost = await update_paid_api_keys_states(app, filter_api_key)
    else:
        credits, total_cost = None, None

    query_details = QueryDetails(
        start_datetime=start_datetime_detail,
        end_datetime=end_datetime_detail,
        api_key_filter=api_key_filter_detail,
        model_filter=model if model else "all",
        credits="$" + str(credits) if credits is not None else None,
        total_cost="$" + str(total_cost) if total_cost is not None else None,
        balance="$" + str(float(credits) - float(total_cost)) if credits and total_cost else None
    )

    response_data = TokenUsageResponse(
        usage=[TokenUsageEntry(**item) for item in usage_data],
        query_details=query_details
    )

    return response_data


@router.get(
    "/v1/channel_key_rankings",
    response_model=ChannelKeyRankingsResponse,
    dependencies=[Depends(rate_limit_dependency)],
)
async def get_channel_key_rankings(
    request: Request,
    provider_name: str,
    start_datetime: Optional[str] = None,
    end_datetime: Optional[str] = None,
    last_n_days: Optional[int] = None,
    token: str = Depends(verify_admin_api_key),
):
    """
    获取特定渠道的 API key 成功率排名，可按时间范围过滤。
    """
    if DISABLE_DATABASE:
        raise HTTPException(status_code=503, detail="Database is disabled.")

    end_dt_obj = None
    start_dt_obj = None
    start_datetime_detail = None
    end_datetime_detail = None
    now = datetime.now(timezone.utc)

    if last_n_days is not None:
        if start_datetime or end_datetime:
            raise HTTPException(
                status_code=400,
                detail="Cannot use last_n_days with start_datetime or end_datetime.",
            )
        if last_n_days <= 0:
            raise HTTPException(status_code=400, detail="last_n_days must be positive.")
        start_dt_obj = now - timedelta(days=last_n_days)
        end_dt_obj = now
        start_datetime_detail = start_dt_obj.isoformat(timespec="seconds")
        end_datetime_detail = end_dt_obj.isoformat(timespec="seconds")
    elif start_datetime or end_datetime:
        try:
            if start_datetime:
                start_dt_obj = parse_datetime_input(start_datetime)
                start_datetime_detail = start_dt_obj.isoformat(timespec="seconds")
            if end_datetime:
                end_dt_obj = parse_datetime_input(end_datetime)
                end_datetime_detail = end_dt_obj.isoformat(timespec="seconds")
            if start_dt_obj and end_dt_obj and end_dt_obj < start_dt_obj:
                raise HTTPException(
                    status_code=400, detail="end_datetime cannot be before start_datetime."
                )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        start_dt_obj = now - timedelta(days=1)
        end_dt_obj = now
        start_datetime_detail = start_dt_obj.isoformat(timespec="seconds")
        end_datetime_detail = end_dt_obj.isoformat(timespec="seconds")

    rankings_data = await query_channel_key_stats(
        provider_name=provider_name, start_dt=start_dt_obj, end_dt=end_dt_obj
    )

    query_details = QueryDetails(
        start_datetime=start_datetime_detail,
        end_datetime=end_datetime_detail,
        api_key_filter=provider_name,
    )

    response_data = ChannelKeyRankingsResponse(
        rankings=[ChannelKeyRanking(**item) for item in rankings_data],
        query_details=query_details,
    )

    return response_data


@router.get("/v1/api_keys_states", dependencies=[Depends(rate_limit_dependency)])
async def api_keys_states(token: str = Depends(verify_admin_api_key)):
    """
    获取所有付费 API key 的状态
    """
    app = get_app()
    
    states_dict = {}
    # 修改原因：统一配额上线后仍要保留旧 credits 响应，避免旧前端或旧脚本读取 api_keys_states 失败。
    # 修改方式：原 paid_api_keys_states 循环保持不变，只在后面额外附加 quota_states 字段。
    # 目的：让 Phase 2 前端可以读取新 quota 状态，同时保持 /v1/api_keys_states 的旧结构兼容。
    for key, state in app.state.paid_api_keys_states.items():
        states_dict[key] = ApiKeyState(
            credits=state["credits"],
            created_at=state["created_at"],
            all_tokens_info=state["all_tokens_info"],
            total_cost=state["total_cost"],
            enabled=state["enabled"]
        )

    quota_states = {}
    quota_registry = getattr(app.state, 'quota_registry', None)
    if quota_registry:
        for kc in (app.state.config or {}).get('api_keys', []):
            api = kc.get('api', '')
            if api and quota_registry.has_quota(api):
                quota_states[api] = quota_registry.get_key_status(api)

    response = ApiKeysStatesResponse(api_keys_states=states_dict)
    resp_dict = response.model_dump() if hasattr(response, 'model_dump') else response.dict()
    resp_dict['quota_states'] = quota_states
    return resp_dict


@router.post("/v1/api_keys/quota/reset", dependencies=[Depends(rate_limit_dependency)])
async def reset_api_key_quota(
    payload: QuotaResetRequest = Body(...),
    token: str = Depends(verify_admin_api_key),
):
    """手动重置单个 API Key 的一条统一配额状态。"""

    # 修改原因：Admin 的 Key 编辑面板需要按 Scope × Metric 正交状态手动清零额度。
    # 修改方式：通过 admin-only 接口接收 api_key 和 quota_states 中的 status_key，调用 QuotaRegistry 清零内存计数。
    # 目的：管理员无需重启服务或修改配置，就可以重置 Key 级、Per-IP、模型级等单条额度状态。
    app = get_app()
    api_key = (payload.api_key or '').strip()
    status_key = (payload.status_key or '').strip()
    if not api_key or not status_key:
        raise HTTPException(status_code=400, detail="api_key and status_key are required")

    quota_registry = getattr(app.state, 'quota_registry', None)
    if not quota_registry or not quota_registry.has_quota(api_key):
        raise HTTPException(status_code=404, detail="Quota counter not found for this API key")

    try:
        reset_result = quota_registry.reset_key_status(api_key, status_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError:
        raise HTTPException(status_code=404, detail="Quota counter not found for this API key")

    return JSONResponse(content={
        "success": True,
        "reset": reset_result,
        "quota_state": quota_registry.get_key_status(api_key),
    })


@router.post("/v1/add_credits", dependencies=[Depends(rate_limit_dependency)])
async def add_credits_to_api_key(
    request: Request,
    paid_key: str = Query(..., description="The API key to add credits to"),
    amount: float = Query(..., description="The amount of credits to add. Must be positive.", gt=0),
    token: str = Depends(verify_admin_api_key)
):
    """
    为指定的 API key 添加额度
    """
    from core.log_config import logger
    from utils import update_config, save_config_to_db, save_api_yaml
    from core.env import env_bool
    import os
    
    app = get_app()
    
    if paid_key not in app.state.paid_api_keys_states:
        raise HTTPException(
            status_code=404,
            detail=f"API key '{paid_key}' not found in paid API keys states."
        )

    app.state.paid_api_keys_states[paid_key]["credits"] += float(amount)

    # 持久化：同步回写 app.state.config 中的 credits
    try:
        api_list = getattr(app.state, 'api_list', []) or []
        if paid_key in api_list:
            key_index = api_list.index(paid_key)
            api_keys = app.state.config.get('api_keys', [])
            if key_index < len(api_keys) and isinstance(api_keys[key_index], dict):
                if 'preferences' not in api_keys[key_index]:
                    api_keys[key_index]['preferences'] = {}
                api_keys[key_index]['preferences']['credits'] = app.state.paid_api_keys_states[paid_key]["credits"]
                # 首次设置 credits 时自动写入计费起始时间
                if not api_keys[key_index]['preferences'].get('created_at'):
                    from datetime import datetime, timezone
                    api_keys[key_index]['preferences']['created_at'] = datetime.now(timezone.utc)

                config_storage = (os.getenv("CONFIG_STORAGE") or "file").strip().lower()
                if config_storage in ("file", "auto") or env_bool("SYNC_CONFIG_TO_FILE", False):
                    save_api_yaml(app.state.config)
                if config_storage in ("auto", "db"):
                    await save_config_to_db(app.state.config)
    except Exception as e:
        logger.warning(f"Failed to persist credits change: {e}")

    current_credits = app.state.paid_api_keys_states[paid_key]["credits"]
    total_cost = app.state.paid_api_keys_states[paid_key]["total_cost"]
    app.state.paid_api_keys_states[paid_key]["enabled"] = current_credits >= total_cost

    logger.info(
        f"Credits for API key '{paid_key}' updated. "
        f"Amount added: {amount}, New credits: {current_credits}, "
        f"Enabled: {app.state.paid_api_keys_states[paid_key]['enabled']}"
    )

    return JSONResponse(content={
        "message": f"Successfully added {amount} credits to API key '{paid_key}'.",
        "paid_key": paid_key,
        "new_credits": current_credits,
        "enabled": app.state.paid_api_keys_states[paid_key]["enabled"]
    })


@router.post("/v1/logs/cleanup", response_model=LogsCleanupResponse, dependencies=[Depends(rate_limit_dependency)])
async def cleanup_logs(
    request: Request,
    payload: LogsCleanupRequest,
    token: str = Depends(verify_admin_api_key),
):
    """按条件清理日志数据。

    支持两种模式：
    - clear_fields：清空日志中的大字段，保留日志行（推荐）
    - delete_rows：删除匹配日志行（危险操作）
    """

    if DISABLE_DATABASE:
        raise HTTPException(status_code=503, detail="Database is disabled.")

    action, selected_fields = _validate_cleanup_request(payload)
    cutoff_dt, start_dt, end_dt, filters = _build_cleanup_time_filters(payload)

    if payload.provider:
        filters["provider"] = payload.provider
    if payload.api_key:
        filters["api_key"] = payload.api_key
    if payload.model:
        filters["model"] = payload.model
    if payload.success is not None:
        filters["success"] = payload.success
    if payload.status_codes:
        filters["status_codes"] = sorted(set(payload.status_codes))
    if payload.flagged_only:
        filters["flagged_only"] = True

    db_type = (DB_TYPE or "sqlite").lower()

    # ========== D1 分支 ==========
    if db_type == "d1":
        from db import d1_client

        if d1_client is None:
            raise HTTPException(status_code=503, detail="D1 client is not initialized.")

        where_sql_parts: List[str] = ["1=1"]
        params: List[Any] = []

        if cutoff_dt is not None:
            where_sql_parts.append("timestamp < ?")
            params.append(cutoff_dt)
        if start_dt is not None:
            where_sql_parts.append("timestamp >= ?")
            params.append(start_dt)
        if end_dt is not None:
            where_sql_parts.append("timestamp <= ?")
            params.append(end_dt)

        if payload.provider:
            like_value = f"%{payload.provider}%"
            where_sql_parts.append("(provider_id LIKE ? OR provider LIKE ?)")
            params.extend([like_value, like_value])

        if payload.api_key:
            like_value = f"%{payload.api_key}%"
            where_sql_parts.append("(api_key_name LIKE ? OR api_key_group LIKE ? OR api_key LIKE ?)")
            params.extend([like_value, like_value, like_value])

        if payload.model:
            where_sql_parts.append("model LIKE ?")
            params.append(f"%{payload.model}%")

        if payload.success is not None:
            where_sql_parts.append("success = ?")
            params.append(1 if payload.success else 0)

        if payload.status_codes:
            placeholders = ", ".join(["?"] * len(payload.status_codes))
            where_sql_parts.append(f"status_code IN ({placeholders})")
            params.extend(payload.status_codes)

        if payload.flagged_only:
            where_sql_parts.append("is_flagged = 1")

        count_fragments = ["COUNT(*) AS matched_rows"]
        for field in selected_fields:
            count_fragments.append(f"SUM(CASE WHEN {field} IS NOT NULL THEN 1 ELSE 0 END) AS {field}")

        count_sql = f"SELECT {', '.join(count_fragments)} FROM request_stats WHERE {' AND '.join(where_sql_parts)}"
        count_row = await d1_client.query_one(count_sql, params)
        count_row = count_row or {}

        matched_rows = int(count_row.get("matched_rows") or 0)
        non_null_counts = {field: int(count_row.get(field) or 0) for field in selected_fields}

        if payload.dry_run:
            return LogsCleanupResponse(
                dry_run=True,
                action=action,
                matched_rows=matched_rows,
                affected_rows=0,
                selected_fields=selected_fields,
                non_null_counts=non_null_counts,
                filters=filters,
                message="Dry run completed. No changes have been applied.",
            )

        affected_rows = 0
        if action == "clear_fields":
            set_clause = ", ".join([f"{field} = NULL" for field in selected_fields])
            update_where_parts = list(where_sql_parts)
            if selected_fields:
                update_where_parts.append("(" + " OR ".join([f"{field} IS NOT NULL" for field in selected_fields]) + ")")

            sql = f"UPDATE request_stats SET {set_clause} WHERE {' AND '.join(update_where_parts)}"
            result = await d1_client.execute(sql, params)
            affected_rows = int((result.get("meta") or {}).get("changes") or 0)
        else:
            sql = f"DELETE FROM request_stats WHERE {' AND '.join(where_sql_parts)}"
            result = await d1_client.execute(sql, params)
            affected_rows = int((result.get("meta") or {}).get("changes") or 0)

        return LogsCleanupResponse(
            dry_run=False,
            action=action,
            matched_rows=matched_rows,
            affected_rows=affected_rows,
            selected_fields=selected_fields,
            non_null_counts=non_null_counts,
            filters=filters,
            message="Cleanup applied successfully.",
        )

    # ========== SQLAlchemy 分支（sqlite/postgres/mysql） ==========
    conditions = []
    if cutoff_dt is not None:
        conditions.append(RequestStat.timestamp < cutoff_dt)
    if start_dt is not None:
        conditions.append(RequestStat.timestamp >= start_dt)
    if end_dt is not None:
        conditions.append(RequestStat.timestamp <= end_dt)

    if payload.provider:
        conditions.append(
            or_(
                RequestStat.provider_id.ilike(f"%{payload.provider}%"),
                RequestStat.provider.ilike(f"%{payload.provider}%"),
            )
        )

    if payload.api_key:
        conditions.append(
            or_(
                RequestStat.api_key_name.ilike(f"%{payload.api_key}%"),
                RequestStat.api_key_group.ilike(f"%{payload.api_key}%"),
                RequestStat.api_key.ilike(f"%{payload.api_key}%"),
            )
        )

    if payload.model:
        conditions.append(RequestStat.model.ilike(f"%{payload.model}%"))

    if payload.success is not None:
        conditions.append(RequestStat.success == payload.success)

    if payload.status_codes:
        conditions.append(RequestStat.status_code.in_(payload.status_codes))

    if payload.flagged_only:
        conditions.append(RequestStat.is_flagged.is_(True))

    async with async_session_scope() as session:
        aggregate_cols = [func.count(RequestStat.id).label("matched_rows")]
        for field in selected_fields:
            column = getattr(RequestStat, field)
            aggregate_cols.append(func.sum(case((column.isnot(None), 1), else_=0)).label(field))

        count_query = select(*aggregate_cols).where(*conditions)
        count_result = await session.execute(count_query)
        count_row = count_result.mappings().one_or_none() or {}

        matched_rows = int(count_row.get("matched_rows") or 0)
        non_null_counts = {field: int(count_row.get(field) or 0) for field in selected_fields}

        if payload.dry_run:
            return LogsCleanupResponse(
                dry_run=True,
                action=action,
                matched_rows=matched_rows,
                affected_rows=0,
                selected_fields=selected_fields,
                non_null_counts=non_null_counts,
                filters=filters,
                message="Dry run completed. No changes have been applied.",
            )

        if action == "clear_fields":
            values_dict = {field: None for field in selected_fields}
            non_null_clause = or_(*[getattr(RequestStat, field).isnot(None) for field in selected_fields])
            stmt = update(RequestStat).where(*conditions).where(non_null_clause).values(**values_dict)
        else:
            stmt = delete(RequestStat).where(*conditions)

        exec_result = await session.execute(stmt)
        await session.commit()

        raw_rowcount = exec_result.rowcount
        affected_rows = int(raw_rowcount if isinstance(raw_rowcount, int) and raw_rowcount >= 0 else matched_rows)

        return LogsCleanupResponse(
            dry_run=False,
            action=action,
            matched_rows=matched_rows,
            affected_rows=affected_rows,
            selected_fields=selected_fields,
            non_null_counts=non_null_counts,
            filters=filters,
            message="Cleanup applied successfully.",
        )


@router.get("/v1/logs", response_model=LogsPage, dependencies=[Depends(rate_limit_dependency)])
async def get_logs(
    request: Request,
    page: int = Query(1, ge=1, description="Page number (starting from 1)"),
    page_size: int = Query(20, ge=1, le=200, description="Number of items per page"),
    start_time: Optional[str] = Query(None, description="Start time filter (ISO 8601 or Unix timestamp)"),
    end_time: Optional[str] = Query(None, description="End time filter (ISO 8601 or Unix timestamp)"),
    provider: Optional[str] = Query(None, description="Provider/channel filter (fuzzy match)"),
    api_key: Optional[str] = Query(None, description="API key/token filter (fuzzy match)"),
    model: Optional[str] = Query(None, description="Model name filter (fuzzy match)"),
    success: Optional[bool] = Query(None, description="Filter by success status"),
    token: str = Depends(verify_admin_api_key),
):
    """
    获取请求日志（RequestStat）分页列表，仅管理员可访问。
    支持时间范围筛选和模糊搜索。
    """
    if DISABLE_DATABASE:
        raise HTTPException(status_code=503, detail="Database is disabled.")

    if (DB_TYPE or "sqlite").lower() == "d1":
        from db import d1_client
        if d1_client is None:
            return LogsPage(items=[], total=0, page=page, page_size=page_size, total_pages=0)

        # 修改原因：D1/SQLite 列表分支原来 SELECT * 并额外 COUNT，会读取大字段且重复扫描。
        # 修改方式：使用轻量列清单和 COUNT(*) OVER()，total 随当前页数据一起返回。
        # 目的：让 /v1/logs 列表只承担摘要查询，展开详情再访问 /v1/logs/{id} 拉取完整行。
        sql = _build_d1_logs_list_sql()
        params: list[Any] = []

        if start_time:
            try:
                start_dt = parse_datetime_input(start_time)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid start_time: {e}")
            sql += " AND timestamp >= ?"
            params.append(format_d1_datetime(start_dt))

        if end_time:
            try:
                end_dt = parse_datetime_input(end_time)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid end_time: {e}")
            sql += " AND timestamp <= ?"
            params.append(format_d1_datetime(end_dt))

        if provider:
            like_value = f"%{provider}%"
            sql += " AND (provider_id LIKE ? OR provider LIKE ?)"
            params.extend([like_value, like_value])

        if api_key:
            like_value = f"%{api_key}%"
            sql += " AND (api_key_name LIKE ? OR api_key_group LIKE ? OR api_key LIKE ?)"
            params.extend([like_value, like_value, like_value])

        if model:
            like_value = f"%{model}%"
            sql += " AND model LIKE ?"
            params.append(like_value)

        if success is not None:
            success_value = 1 if success else 0
            sql += " AND success = ?"
            params.append(success_value)

        # 先查 COUNT（轻量，不带大字段）
        count_sql = sql.replace(f"SELECT {LOG_LIST_SQL_COLUMN_CLAUSE}", "SELECT COUNT(*) AS total", 1)
        total = int(await d1_client.query_value(count_sql, params, column="total", default=0) or 0)
        if total == 0:
            return LogsPage(items=[], total=0, page=page, page_size=page_size, total_pages=0)

        offset = (page - 1) * page_size
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        rows = await d1_client.query_all(sql, [*params, page_size, offset])

        if not rows:
            return LogsPage(items=[], total=0, page=page, page_size=page_size, total_pages=0)
        total_pages = (total + page_size - 1) // page_size if total > 0 else 0
        now = datetime.now(timezone.utc)
        items = [
            _log_entry_from_mapping(row, include_raw_fields=False, now=now)
            for row in rows
        ]

        return LogsPage(
            items=items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async with async_session_scope() as session:
        # 构建基础查询条件
        conditions = []

        # 时间筛选
        if start_time:
            try:
                start_dt = parse_datetime_input(start_time)
                conditions.append(RequestStat.timestamp >= start_dt)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid start_time: {e}")

        if end_time:
            try:
                end_dt = parse_datetime_input(end_time)
                conditions.append(RequestStat.timestamp <= end_dt)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid end_time: {e}")

        # 模糊搜索：渠道（兼容 provider_id 与 provider 字段）
        if provider:
            conditions.append(
                or_(
                    RequestStat.provider_id.ilike(f"%{provider}%"),
                    RequestStat.provider.ilike(f"%{provider}%")
                )
            )

        # 模糊搜索：令牌（API key 名称或分组，及原始 api_key）
        if api_key:
            conditions.append(
                or_(
                    RequestStat.api_key_name.ilike(f"%{api_key}%"),
                    RequestStat.api_key_group.ilike(f"%{api_key}%"),
                    RequestStat.api_key.ilike(f"%{api_key}%")
                )
            )

        # 模型名模糊匹配
        if model:
            conditions.append(RequestStat.model.ilike(f"%{model}%"))

        # 成功/失败筛选
        if success is not None:
            conditions.append(RequestStat.success == success)

        offset = (page - 1) * page_size

        # 先查 COUNT（轻量，走索引）
        count_query = select(func.count()).where(*conditions)
        total = (await session.execute(count_query)).scalar() or 0
        if total == 0:
            return LogsPage(items=[], total=0, page=page, page_size=page_size, total_pages=0)

        # 再查轻量列（不含 body 大字段）
        query = (
            select(*_log_list_sa_columns())
            .where(*conditions)
            .order_by(RequestStat.timestamp.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows_result = await session.execute(query)
        rows = rows_result.mappings().all()

    if not rows:
        return LogsPage(items=[], total=0, page=page, page_size=page_size, total_pages=0)

    total = total
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    now = datetime.now(timezone.utc)
    items = [
        _log_entry_from_mapping(row, include_raw_fields=False, now=now)
        for row in rows
    ]

    return LogsPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/v1/logs/{log_id}", response_model=LogEntry, dependencies=[Depends(rate_limit_dependency)])
async def get_log_detail(
    request: Request,
    log_id: int,
    token: str = Depends(verify_admin_api_key),
):
    """
    获取单条请求日志完整详情，仅管理员可访问。
    """
    if DISABLE_DATABASE:
        raise HTTPException(status_code=503, detail="Database is disabled.")

    if (DB_TYPE or "sqlite").lower() == "d1":
        from db import d1_client
        if d1_client is None:
            raise HTTPException(status_code=404, detail="Log not found.")

        # 修改原因：列表接口已经排除原始大字段，展开详情时才需要读取完整日志行。
        # 修改方式：单条详情端点按 id 执行 SELECT *，只对用户展开的那一条日志读取 body 和 headers。
        # 目的：把高成本大字段读取从列表分页路径移到按需详情路径。
        rows = await d1_client.query_all(
            "SELECT * FROM request_stats WHERE id = ? LIMIT 1",
            [log_id],
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Log not found.")
        return _log_entry_from_mapping(rows[0], include_raw_fields=True)

    async with async_session_scope() as session:
        # 修改原因：SQLAlchemy 详情接口需要返回完整字段，但仍限定为单个主键，避免列表查询拉取大字段。
        # 修改方式：显式选择 request_stats 的全部列并按 id 限制一行。
        # 目的：保持详情展示能力不变，同时让列表接口维持轻量查询。
        query = (
            select(*_log_detail_sa_columns())
            .where(RequestStat.id == log_id)
            .limit(1)
        )
        result = await session.execute(query)
        row = result.mappings().one_or_none()

    if row is None:
        raise HTTPException(status_code=404, detail="Log not found.")
    return _log_entry_from_mapping(row, include_raw_fields=True)


# ==================== 后台日志 & 出站请求日志 ====================


@router.get("/v1/backend_logs", dependencies=[Depends(rate_limit_dependency)])
async def get_backend_logs(
    request: Request,
    since_id: Optional[int] = Query(None, description="Only return entries with id > since_id"),
    limit: int = Query(200, ge=1, le=2000, description="Max entries to return"),
    search: Optional[str] = Query(None, description="Search keyword (case-insensitive)"),
    stream: Optional[str] = Query(None, description="Filter by stream: stdout or stderr"),
    level: Optional[str] = Query(None, description="Filter by exact log level: DEBUG/INFO/WARNING/ERROR/CRITICAL"),
    level_group: Optional[str] = Query(None, description="Filter by level group: errors (ERROR+CRITICAL)"),
    logger_name: Optional[str] = Query(None, description="Filter by logger name (exact, case-insensitive)"),
    token: str = Depends(verify_admin_api_key),
):
    """
    获取后台进程日志（stdout/stderr 内存缓冲区）。
    仅管理员可访问，不依赖数据库。
    """
    from core.log_config import get_backend_log_entries

    result = get_backend_log_entries(
        since_id=since_id,
        limit=limit,
        search=search,
        stream=stream,
        level=level,
        level_group=level_group,
        logger_name=logger_name,
    )

    # 将 datetime 对象转为 ISO 字符串
    for item in result.get("items", []):
        if hasattr(item.get("captured_at"), "isoformat"):
            item["captured_at"] = item["captured_at"].isoformat()

    return JSONResponse(content=result)


@router.get("/v1/outbound_logs", dependencies=[Depends(rate_limit_dependency)])
async def get_outbound_logs(
    request: Request,
    since_id: Optional[int] = Query(None, description="Only return entries with id > since_id"),
    limit: int = Query(200, ge=1, le=2000, description="Max entries to return"),
    host: Optional[str] = Query(None, description="Filter by target host (fuzzy)"),
    method: Optional[str] = Query(None, description="Filter by HTTP method: GET/POST/..."),
    status_min: Optional[int] = Query(None, description="Min status code (inclusive)"),
    status_max: Optional[int] = Query(None, description="Max status code (inclusive)"),
    search: Optional[str] = Query(None, description="Search keyword in URL (case-insensitive)"),
    token: str = Depends(verify_admin_api_key),
):
    """
    获取后端出站 HTTP 请求日志（内存缓冲区）。
    记录所有通过 httpx.AsyncClient 发出的请求。
    仅管理员可访问，不依赖数据库。
    """
    from core.http import get_outbound_log_entries

    result = get_outbound_log_entries(
        since_id=since_id,
        limit=limit,
        host=host,
        method=method,
        status_min=status_min,
        status_max=status_max,
        search=search,
    )
    return JSONResponse(content=result)


# ── 内存级 provider 活跃度缓存 ──
import time as _time
_provider_last_seen: dict[str, float] = {}  # provider → unix timestamp
_activity_warmed = False

def record_provider_activity(provider: str):
    """每次请求经过时调用，O(1) 写入内存"""
    if provider:
        _provider_last_seen[provider] = _time.time()

async def warm_provider_activity():
    """启动时从 DB 预热缓存（后台执行，不阻塞启动）"""
    global _activity_warmed
    try:
        from db import DISABLE_DATABASE, DB_TYPE
        if DISABLE_DATABASE:
            _activity_warmed = True
            return
        if (DB_TYPE or "sqlite").lower() == "d1":
            _activity_warmed = True
            return
        from db import async_session_scope, RequestStat
        from sqlalchemy import func, select
        async with async_session_scope() as session:
            stmt = select(
                RequestStat.provider,
                func.max(RequestStat.timestamp).label("last_active")
            ).group_by(RequestStat.provider)
            result = await session.execute(stmt)
            for row in result.fetchall():
                provider = row[0]
                last_active = row[1]
                if provider and last_active:
                    ts = last_active.timestamp() if hasattr(last_active, 'timestamp') else _time.time()
                    # 只填充还没有的（运行时记录优先）
                    if provider not in _provider_last_seen:
                        _provider_last_seen[provider] = ts
        _activity_warmed = True
        import logging
        logging.getLogger(__name__).info(f"[provider_activity] Warmed cache from DB: {len(_provider_last_seen)} providers")
    except Exception as e:
        _activity_warmed = True
        import logging
        logging.getLogger(__name__).warning(f"[provider_activity] Warm failed: {e}")

# 每日活跃度刷新已移至 main.py 统一 daily_maintenance 循环

@router.get("/v1/stats/provider_activity", dependencies=[Depends(rate_limit_dependency)])
async def provider_activity():
    """
    返回每个 provider 的最后活跃时间（从内存缓存读取，秒回）。
    Returns: {"activity": {"provider_name": 1714567890.123, ...}, "warmed": true}
    """
    return JSONResponse(content={"activity": _provider_last_seen, "warmed": _activity_warmed})


@router.post("/v1/stats/resolve_prices", dependencies=[Depends(rate_limit_dependency)])
async def resolve_prices(request: Request):
    """
    批量查询模型价格。走完整 6 层级联（渠道 > 全局 > 外部库 > default > 0）。
    
    Body: {"models": [{"model": "gpt-4o", "provider": "openai"}, ...]}
    Returns: {"prices": {"gpt-4o": {"prompt": 2.5, "completion": 10.0}, ...}}
    """
    from core.stats import get_current_model_prices
    app = get_app()
    body = await request.json()
    models = body.get("models", [])
    
    prices = {}
    for item in models:
        if isinstance(item, str):
            model_name = item
            provider_name = None
        elif isinstance(item, dict):
            model_name = item.get("model", "")
            provider_name = item.get("provider")
        else:
            continue
        if not model_name or model_name in prices:
            continue
        # 修改原因：价格解析现在会返回 cached_price，批量解析接口目前只承诺 prompt 和 completion。
        # 修改方式：解包第三段但不写入响应，避免改变前端或外部调用方的响应结构。
        # 目的：兼容三元组返回值，并维持 resolve_prices 的旧接口契约。
        prompt_price, completion_price, _ = get_current_model_prices(
            app, model_name, provider_name=provider_name
        )
        prices[model_name] = {"prompt": prompt_price, "completion": completion_price}
    
    return JSONResponse(content={"prices": prices})
