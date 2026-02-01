"""
数据库统计模块

负责：
- 数据库表初始化和迁移
- 请求统计写入 (RequestStat)
- 渠道统计写入 (ChannelStat)
- Token 使用量查询和聚合
- 成本计算
"""

import os
import asyncio

from core.env import env_bool
from asyncio import Semaphore
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

from pydantic import BaseModel, field_serializer
from sqlalchemy import inspect, text, func, select
from sqlalchemy.sql import sqltypes
from sqlalchemy.ext.asyncio import AsyncSession

from core.log_config import logger
from db import Base, RequestStat, ChannelStat, AppConfig, AdminUser, db_engine, async_session, DISABLE_DATABASE, DB_TYPE

# SQLite 写入重试配置
SQLITE_MAX_RETRIES = 3
SQLITE_RETRY_DELAY = 0.5  # 初始重试延迟（秒）

is_debug = env_bool("DEBUG", False)

# 根据数据库类型，动态创建信号量
# SQLite 需要严格的串行写入，而 PostgreSQL 可以处理高并发
# 这里使用 db.py 的解析结果（支持 DATABASE_URL 自动识别）
if (DB_TYPE or "sqlite").lower() == 'sqlite':
    db_semaphore = Semaphore(1)
    logger.info("Database semaphore configured for SQLite (1 concurrent writer).")
else:  # For postgres
    # 允许50个并发写入操作，这对于PostgreSQL来说是合理的
    db_semaphore = Semaphore(50)
    logger.info("Database semaphore configured for PostgreSQL (50 concurrent writers).")


# ============== Pydantic Models ==============

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


# ============== 数据库初始化 ==============

def _map_sa_type_to_sql_type(sa_type):
    """将 SQLAlchemy 类型映射到 SQL 类型字符串"""
    type_map = {
        sqltypes.Integer: "INTEGER",
        sqltypes.String: "TEXT",
        sqltypes.Float: "REAL",
        sqltypes.Boolean: "BOOLEAN",
        sqltypes.DateTime: "DATETIME",
        sqltypes.Text: "TEXT"
    }
    return type_map.get(type(sa_type), "TEXT")


def _get_default_sql(default):
    """生成列默认值的 SQL 片段"""
    if default is None:
        return ""
    if isinstance(default.arg, bool):
        return f" DEFAULT {str(default.arg).upper()}"
    if isinstance(default.arg, (int, float)):
        return f" DEFAULT {default.arg}"
    if isinstance(default.arg, str):
        return f" DEFAULT '{default.arg}'"
    return ""


async def create_tables():
    """创建数据库表并执行简易列迁移"""
    if DISABLE_DATABASE:
        return
    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 检查并添加缺失的列 - 扩展此简易迁移以支持 SQLite 和 PostgreSQL
        db_type = (DB_TYPE or "sqlite").lower()
        if db_type in ["sqlite", "postgres"]:
            def check_and_add_columns(connection):
                inspector = inspect(connection)
                for table in [RequestStat, ChannelStat, AppConfig, AdminUser]:
                    table_name = table.__tablename__
                    existing_columns = {col['name'] for col in inspector.get_columns(table_name)}

                    for column_name, column in table.__table__.columns.items():
                        if column_name not in existing_columns:
                            # 适配 PostgreSQL / SQLite 的类型映射
                            # 注意：JSON/JSONB 在不同方言下 compile 结果不同，
                            # 且 CockroachDB 对 JSONB 的兼容也可能返回 JSON。
                            col_type = column.type.compile(connection.dialect)

                            # SQLite 允许 DEFAULT；Postgres/Cockroach 对 JSON 默认值较敏感，这里统一不加默认
                            default = _get_default_sql(column.default) if db_type == "sqlite" else ""

                            # 使用标准的 ALTER TABLE 语法
                            connection.execute(
                                text(
                                    f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {col_type}{default}'
                                )
                            )
                            logger.info(
                                f"Added column '{column_name}' ({col_type}) to table '{table_name}'."
                            )

            await conn.run_sync(check_and_add_columns)


# ============== 成本计算 ==============

def get_current_model_prices(app, model_name: str):
    """
    根据当前配置偏好，返回指定模型的 prompt_price 和 completion_price（单位：$/M tokens）
    
    Args:
        app: FastAPI 应用实例
        model_name: 模型名称
    
    Returns:
        (prompt_price, completion_price) 元组
    """
    from utils import safe_get
    try:
        model_price = safe_get(app.state.config, 'preferences', "model_price", default={})
        price_str = next((model_price[k] for k in model_price.keys() if model_name and model_name.startswith(k)), model_price.get("default", "0.3,1"))
        parts = [p.strip() for p in str(price_str).split(",")]
        prompt_price = float(parts[0]) if len(parts) > 0 and parts[0] != "" else 0.3
        completion_price = float(parts[1]) if len(parts) > 1 and parts[1] != "" else 1.0
        return prompt_price, completion_price
    except Exception:
        return 0.3, 1.0


async def compute_total_cost_from_db(filter_api_key: Optional[str] = None, start_dt_obj: Optional[datetime] = None) -> float:
    """
    直接从数据库历史记录累计成本：
    sum((prompt_tokens*prompt_price + completion_tokens*completion_price)/1e6)
    """
    if DISABLE_DATABASE:
        return 0.0
    async with async_session() as session:
        expr = (func.coalesce(RequestStat.prompt_tokens, 0) * func.coalesce(RequestStat.prompt_price, 0.3) + func.coalesce(RequestStat.completion_tokens, 0) * func.coalesce(RequestStat.completion_price, 1.0)) / 1000000.0
        query = select(func.coalesce(func.sum(expr), 0.0))
        if filter_api_key:
            query = query.where(RequestStat.api_key == filter_api_key)
        if start_dt_obj:
            query = query.where(RequestStat.timestamp >= start_dt_obj)
        result = await session.execute(query)
        total_cost = result.scalar_one() or 0.0
        try:
            total_cost = float(total_cost)
        except Exception:
            total_cost = 0.0
        return total_cost


# ============== 统计写入 ==============

async def update_stats(current_info: dict, app=None, get_model_prices_func=None):
    """
    更新请求统计到数据库
    
    Args:
        current_info: 包含请求信息的字典
        app: FastAPI 应用实例（用于获取模型价格）
        get_model_prices_func: 获取模型价格的函数，签名为 (model_name) -> (prompt_price, completion_price)
    """
    if DISABLE_DATABASE:
        return

    # 在成功请求时，快照当前价格，写入数据库
    try:
        if current_info.get("success") and current_info.get("model"):
            if get_model_prices_func:
                prompt_price, completion_price = get_model_prices_func(current_info["model"])
            elif app:
                prompt_price, completion_price = get_current_model_prices(app, current_info["model"])
            else:
                prompt_price, completion_price = 0.3, 1.0
            current_info["prompt_price"] = prompt_price
            current_info["completion_price"] = completion_price
    except Exception:
        pass

    # 使用重试机制写入数据库
    for attempt in range(SQLITE_MAX_RETRIES):
        try:
            # 等待获取数据库访问权限
            async with db_semaphore:
                async with async_session() as session:
                    async with session.begin():
                        columns = [column.key for column in RequestStat.__table__.columns]
                        filtered_info = {k: v for k, v in current_info.items() if k in columns}

                        # 清洗字符串中的 NUL 字符，防止 PostgreSQL 报错
                        for key, value in filtered_info.items():
                            if isinstance(value, str):
                                filtered_info[key] = value.replace('\x00', '')

                        new_request_stat = RequestStat(**filtered_info)
                        session.add(new_request_stat)
                        await session.commit()

            # 检查付费 API 密钥状态更新
            check_key = current_info.get("api_key")
            if app and check_key and hasattr(app.state, 'paid_api_keys_states'):
                if check_key in app.state.paid_api_keys_states and current_info.get("total_tokens", 0) > 0:
                    await update_paid_api_keys_states(app, check_key)
            return  # 成功后直接返回

        except Exception as e:
            error_str = str(e).lower()
            is_lock_error = 'database is locked' in error_str or 'busy' in error_str
            
            if is_lock_error and attempt < SQLITE_MAX_RETRIES - 1:
                # 数据库锁定，等待后重试
                delay = SQLITE_RETRY_DELAY * (2 ** attempt)  # 指数退避
                logger.warning(f"Database locked, retrying in {delay}s (attempt {attempt + 1}/{SQLITE_MAX_RETRIES})")
                await asyncio.sleep(delay)
            else:
                # 最后一次重试失败或非锁定错误
                logger.error(f"Error updating stats: {str(e)}")
                if is_debug:
                    import traceback
                    traceback.print_exc()
                break


async def update_channel_stats(request_id, provider, model, api_key, success, provider_api_key: str = None):
    """更新渠道统计到数据库"""
    if DISABLE_DATABASE:
        return

    # 使用重试机制写入数据库
    for attempt in range(SQLITE_MAX_RETRIES):
        try:
            async with db_semaphore:
                async with async_session() as session:
                    async with session.begin():
                        channel_stat = ChannelStat(
                            request_id=request_id,
                            provider=provider,
                            model=model,
                            api_key=api_key,
                            provider_api_key=provider_api_key,
                            success=success,
                        )
                        session.add(channel_stat)
                        await session.commit()
            return  # 成功后直接返回

        except Exception as e:
            error_str = str(e).lower()
            is_lock_error = 'database is locked' in error_str or 'busy' in error_str
            
            if is_lock_error and attempt < SQLITE_MAX_RETRIES - 1:
                delay = SQLITE_RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Database locked (channel stats), retrying in {delay}s (attempt {attempt + 1}/{SQLITE_MAX_RETRIES})")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Error updating channel stats: {str(e)}")
                if is_debug:
                    import traceback
                    traceback.print_exc()
                break


# ============== Token 使用量查询 ==============

async def query_token_usage(
    session: AsyncSession,
    filter_api_key: Optional[str] = None,
    filter_model: Optional[str] = None,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None
) -> List[Dict]:
    """Queries the RequestStat table for aggregated token usage."""
    query = select(
        RequestStat.api_key,
        RequestStat.model,
        func.sum(RequestStat.prompt_tokens).label("total_prompt_tokens"),
        func.sum(RequestStat.completion_tokens).label("total_completion_tokens"),
        func.sum(RequestStat.total_tokens).label("total_tokens"),
        func.count(RequestStat.id).label("request_count")
    ).group_by(RequestStat.api_key, RequestStat.model)

    # Apply filters
    if filter_api_key:
        query = query.where(RequestStat.api_key == filter_api_key)
    if filter_model:
        query = query.where(RequestStat.model == filter_model)
    if start_dt:
        query = query.where(RequestStat.timestamp >= start_dt)
    if end_dt:
        # Make end_dt inclusive by adding one day
        query = query.where(RequestStat.timestamp < end_dt + timedelta(days=1))

    # Filter out entries with null or empty model if not specifically requested
    if not filter_model:
        query = query.where(RequestStat.model.isnot(None) & (RequestStat.model != ''))

    result = await session.execute(query)
    rows = result.mappings().all()

    # Process results: mask API key
    processed_usage = []
    for row in rows:
        usage_dict = dict(row)
        api_key = usage_dict.get("api_key", "")
        # Mask API key (show prefix like sk-...xyz)
        if api_key and len(api_key) > 7:
            prefix = api_key[:7]
            suffix = api_key[-4:]
            usage_dict["api_key_prefix"] = f"{prefix}...{suffix}"
        else:
            usage_dict["api_key_prefix"] = api_key
        del usage_dict["api_key"]
        processed_usage.append(usage_dict)

    return processed_usage


async def get_usage_data(filter_api_key: Optional[str] = None, filter_model: Optional[str] = None,
                        start_dt_obj: Optional[datetime] = None, end_dt_obj: Optional[datetime] = None) -> List[Dict]:
    """
    查询数据库并获取令牌使用数据。
    这个函数封装了创建会话和查询令牌使用情况的逻辑。

    Args:
        filter_api_key: 可选的API密钥过滤器
        filter_model: 可选的模型过滤器
        start_dt_obj: 开始日期时间
        end_dt_obj: 结束日期时间

    Returns:
        包含令牌使用统计数据的列表
    """
    async with async_session() as session:
        usage_data = await query_token_usage(
            session=session,
            filter_api_key=filter_api_key,
            filter_model=filter_model,
            start_dt=start_dt_obj,
            end_dt=end_dt_obj
        )
    return usage_data


# ============== 付费 API 密钥状态 ==============

async def update_paid_api_keys_states(app, paid_key: str):
    """
    更新付费API密钥的状态

    参数:
        app - FastAPI应用实例
        paid_key - 需要更新状态的API密钥
    
    Returns:
        (credits, total_cost) 元组
    """
    from utils import safe_get
    
    check_index = app.state.api_list.index(paid_key)
    credits = safe_get(app.state.config, 'api_keys', check_index, "preferences", "credits", default=-1)
    created_at = safe_get(app.state.config, 'api_keys', check_index, "preferences", "created_at", default=datetime.now(timezone.utc) - timedelta(days=30))
    created_at = created_at.astimezone(timezone.utc)

    if credits != -1:
        # 仍返回聚合的 token 统计，供前端展示
        all_tokens_info = await get_usage_data(filter_api_key=paid_key, start_dt_obj=created_at)
        # 关键修改：总消耗改为从历史数据逐条累计当时价格
        total_cost = await compute_total_cost_from_db(filter_api_key=paid_key, start_dt_obj=created_at)

        app.state.paid_api_keys_states[paid_key] = {
            "credits": credits,
            "created_at": created_at,
            "all_tokens_info": all_tokens_info,
            "total_cost": total_cost,
            "enabled": True if total_cost <= credits else False
        }
        return credits, total_cost

    return credits, 0