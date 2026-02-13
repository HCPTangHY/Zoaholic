"""
Stats 统计和使用量路由
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_serializer

from sqlalchemy import select, case, func, desc

from db import RequestStat, ChannelStat, async_session_scope, DISABLE_DATABASE, DB_TYPE
from core.stats import get_usage_data
from utils import safe_get, query_channel_key_stats
from routes.deps import rate_limit_dependency, verify_api_key, verify_admin_api_key, get_app
from core.d1_client import parse_d1_datetime

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
    success: bool = False
    status_code: Optional[int] = None
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
    upstream_request_body: Optional[str] = None  # 发送到上游的请求体
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


# ============ Helper Functions ============


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
            "SELECT model, COUNT(*) AS count FROM request_stats "
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
                select(RequestStat.model, func.count().label('count'))
                .where(RequestStat.timestamp >= start_time)
                .group_by(RequestStat.model)
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
        ]
    }

    return JSONResponse(content=stats)


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
    for key, state in app.state.paid_api_keys_states.items():
        states_dict[key] = ApiKeyState(
            credits=state["credits"],
            created_at=state["created_at"],
            all_tokens_info=state["all_tokens_info"],
            total_cost=state["total_cost"],
            enabled=state["enabled"]
        )

    response = ApiKeysStatesResponse(api_keys_states=states_dict)
    return response


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
    
    app = get_app()
    
    if paid_key not in app.state.paid_api_keys_states:
        raise HTTPException(
            status_code=404,
            detail=f"API key '{paid_key}' not found in paid API keys states."
        )

    app.state.paid_api_keys_states[paid_key]["credits"] += float(amount)

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

        sql = "SELECT * FROM request_stats WHERE 1=1"
        count_sql = "SELECT COUNT(*) AS total FROM request_stats WHERE 1=1"
        params: list[Any] = []

        if start_time:
            try:
                start_dt = parse_datetime_input(start_time)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid start_time: {e}")
            sql += " AND timestamp >= ?"
            count_sql += " AND timestamp >= ?"
            params.append(start_dt)

        if end_time:
            try:
                end_dt = parse_datetime_input(end_time)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=f"Invalid end_time: {e}")
            sql += " AND timestamp <= ?"
            count_sql += " AND timestamp <= ?"
            params.append(end_dt)

        if provider:
            like_value = f"%{provider}%"
            sql += " AND (provider_id LIKE ? OR provider LIKE ?)"
            count_sql += " AND (provider_id LIKE ? OR provider LIKE ?)"
            params.extend([like_value, like_value])

        if api_key:
            like_value = f"%{api_key}%"
            sql += " AND (api_key_name LIKE ? OR api_key_group LIKE ? OR api_key LIKE ?)"
            count_sql += " AND (api_key_name LIKE ? OR api_key_group LIKE ? OR api_key LIKE ?)"
            params.extend([like_value, like_value, like_value])

        if model:
            like_value = f"%{model}%"
            sql += " AND model LIKE ?"
            count_sql += " AND model LIKE ?"
            params.append(like_value)

        if success is not None:
            success_value = 1 if success else 0
            sql += " AND success = ?"
            count_sql += " AND success = ?"
            params.append(success_value)

        total = int(await d1_client.query_value(count_sql, params, column="total", default=0) or 0)
        if total == 0:
            return LogsPage(items=[], total=0, page=page, page_size=page_size, total_pages=0)

        total_pages = (total + page_size - 1) // page_size
        if page > total_pages:
            page = total_pages
        offset = (page - 1) * page_size

        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        rows = await d1_client.query_all(sql, [*params, page_size, offset])

        items: List[LogEntry] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            raw_api_key = row.get("api_key") or ""
            if raw_api_key and len(raw_api_key) > 11:
                api_key_prefix = f"{raw_api_key[:7]}...{raw_api_key[-4:]}"
            else:
                api_key_prefix = raw_api_key

            ts = parse_d1_datetime(row.get("timestamp")) or datetime.now(timezone.utc)
            raw_expires_at = parse_d1_datetime(row.get("raw_data_expires_at"))
            raw_data_expired = raw_expires_at is not None and raw_expires_at < now

            items.append(
                LogEntry(
                    id=int(row.get("id") or 0),
                    timestamp=ts,
                    endpoint=row.get("endpoint"),
                    client_ip=row.get("client_ip"),
                    provider=row.get("provider"),
                    model=row.get("model"),
                    api_key_prefix=api_key_prefix,
                    process_time=float(row.get("process_time")) if row.get("process_time") is not None else None,
                    first_response_time=float(row.get("first_response_time")) if row.get("first_response_time") is not None else None,
                    prompt_tokens=int(row.get("prompt_tokens") or 0),
                    completion_tokens=int(row.get("completion_tokens") or 0),
                    total_tokens=int(row.get("total_tokens") or 0),
                    success=_bool_from_db(row.get("success")),
                    status_code=int(row.get("status_code")) if row.get("status_code") is not None else None,
                    is_flagged=_bool_from_db(row.get("is_flagged")),
                    provider_id=row.get("provider_id"),
                    provider_key_index=int(row.get("provider_key_index")) if row.get("provider_key_index") is not None else None,
                    api_key_name=row.get("api_key_name"),
                    api_key_group=row.get("api_key_group"),
                    retry_count=int(row.get("retry_count")) if row.get("retry_count") is not None else None,
                    retry_path=row.get("retry_path") if not raw_data_expired else None,
                    request_headers=row.get("request_headers") if not raw_data_expired else None,
                    request_body=row.get("request_body") if not raw_data_expired else None,
                    upstream_request_body=row.get("upstream_request_body") if not raw_data_expired else None,
                    upstream_response_body=row.get("upstream_response_body") if not raw_data_expired else None,
                    response_body=row.get("response_body") if not raw_data_expired else None,
                    raw_data_expires_at=raw_expires_at,
                )
            )

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
        
        from sqlalchemy import or_
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
        
        # 统计总数
        count_query = select(func.count(RequestStat.id)).where(*conditions)
        result = await session.execute(count_query)
        total = result.scalar() or 0

        if total == 0:
            return LogsPage(
                items=[],
                total=0,
                page=page,
                page_size=page_size,
                total_pages=0,
            )

        total_pages = (total + page_size - 1) // page_size
        if page > total_pages:
            page = total_pages

        offset = (page - 1) * page_size

        query = (
            select(RequestStat)
            .where(*conditions)
            .order_by(RequestStat.timestamp.desc())
            .offset(offset)
            .limit(page_size)
        )
        rows_result = await session.execute(query)
        rows = rows_result.scalars().all()

    items: List[LogEntry] = []
    now = datetime.now(timezone.utc)
    
    for row in rows:
        api_key = row.api_key or ""
        if api_key and len(api_key) > 11:
            prefix = api_key[:7]
            suffix = api_key[-4:]
            api_key_prefix = f"{prefix}...{suffix}"
        else:
            api_key_prefix = api_key

        # 检查原始数据是否过期
        raw_data_expired = False
        if row.raw_data_expires_at:
            # 确保时区一致性：如果数据库时间没有时区信息，将其视为UTC
            expires_at = row.raw_data_expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            raw_data_expired = expires_at < now

        items.append(
            LogEntry(
                id=row.id,
                timestamp=row.timestamp,
                endpoint=row.endpoint,
                client_ip=row.client_ip,
                provider=row.provider,
                model=row.model,
                api_key_prefix=api_key_prefix,
                process_time=row.process_time,
                first_response_time=row.first_response_time,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                total_tokens=row.total_tokens,
                success=row.success if hasattr(row, 'success') else False,
                status_code=row.status_code if hasattr(row, 'status_code') else None,
                is_flagged=row.is_flagged,
                # 扩展日志字段
                provider_id=row.provider_id,
                provider_key_index=row.provider_key_index,
                api_key_name=row.api_key_name,
                api_key_group=row.api_key_group,
                retry_count=row.retry_count,
                retry_path=row.retry_path if not raw_data_expired else None,
                request_headers=row.request_headers if not raw_data_expired else None,
                request_body=row.request_body if not raw_data_expired else None,
                upstream_request_body=getattr(row, 'upstream_request_body', None) if not raw_data_expired else None,
                upstream_response_body=getattr(row, 'upstream_response_body', None) if not raw_data_expired else None,
                response_body=row.response_body if not raw_data_expired else None,
                raw_data_expires_at=row.raw_data_expires_at,
            )
        )

    return LogsPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )