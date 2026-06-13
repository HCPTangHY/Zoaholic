"""
数据库统计模块

负责：
- 数据库表初始化和迁移
- 请求统计写入 (RequestStat)
- 渠道统计写入 (ChannelStat)
- Token 使用量查询和聚合
- 成本计算
"""

import asyncio
from collections import deque

from core.env import env_bool
from asyncio import Semaphore
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any

from pydantic import BaseModel, field_serializer
from sqlalchemy import inspect, text, func, select
from sqlalchemy.sql import sqltypes
from sqlalchemy.ext.asyncio import AsyncSession

from core.log_config import logger
from db import Base, RequestStat, ChannelStat, AppConfig, AdminUser, db_engine, async_session_scope, DISABLE_DATABASE, DB_TYPE
from core.d1_client import format_d1_datetime

# SQLite 写入重试配置
SQLITE_MAX_RETRIES = 3
SQLITE_RETRY_DELAY = 0.5  # 初始重试延迟（秒）

# 修改原因：请求结束路径直接 await update_stats 会在 SQLite 单写信号量前堆积大量协程。
# 修改方式：把 request_stats 写入改为固定长度 deque 缓冲，由单个常驻 consumer 批量写入。
# 目的：限制后台统计写入的协程数量，避免请求高峰或 database locked 重试导致内存持续增长。
_stats_buffer: deque = deque(maxlen=10000)
_stats_consumer_started = False
_stats_flush_event: asyncio.Event | None = None
_STATS_BATCH_SIZE = 20
_STATS_FLUSH_INTERVAL = 2.0

# Prompt Caching 新增列需要在各数据库的简易迁移中显式带 DEFAULT 0，避免旧表新增列后出现 NULL。
PROMPT_CACHE_STAT_COLUMNS = {"cached_tokens", "cache_creation_tokens"}
# 修改原因：日志列表按 timestamp 倒序分页，并常带 provider/model 筛选，单列索引无法充分覆盖这个访问路径。
# 修改方式：集中维护复合索引 SQL，D1 初始化和 SQLite 启动迁移共用同一个定义。
# 目的：减少日志列表排序和过滤的扫描成本，配合列表轻量列查询提升 /v1/logs 性能。
REQUEST_STATS_TS_PROVIDER_MODEL_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_request_stats_ts_provider_model "
    "ON request_stats(timestamp DESC, provider, model)"
)
# 修改原因：Dashboard 的 model_stats 按 model 分组并按 timestamp 过滤，单列 model 索引会回表读取大 TEXT 表中的 timestamp。
# 修改方式：增加 model、timestamp 顺序的覆盖索引，使 SQLite 可以只扫描小索引完成分组和时间过滤。
# 目的：保留 request_stats 口径不变，同时避免 /v1/stats 的模型统计退化为几十秒级查询。
REQUEST_STATS_MODEL_TIMESTAMP_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_request_stats_model_timestamp "
    "ON request_stats(model, timestamp)"
)
# 修改原因：渠道 API key 排名需要通过 request_id 连接 request_stats，旧库没有 request_id 索引会触发 SQLite 临时自动索引。
# 修改方式：增加 request_id 与 token 汇总列的覆盖索引，连接后可直接从索引读取 token 字段。
# 目的：避免 /v1/channel_key_rankings 的 token 聚合每次临时建索引并扫描大表。
REQUEST_STATS_REQUEST_ID_TOKEN_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_request_stats_request_id_tokens "
    "ON request_stats(request_id, prompt_tokens, completion_tokens, total_tokens)"
)
# 修改原因：Dashboard 的 channel_stats 聚合按 provider/model 分组并读取 success，单列索引会扫描并回表。
# 修改方式：增加 provider、model、timestamp、success 覆盖索引，匹配渠道与模型成功率聚合所需字段。
# 目的：让渠道成功率统计从轻量覆盖索引完成，减少临时分组和表页读取。
CHANNEL_STATS_PROVIDER_MODEL_TIMESTAMP_SUCCESS_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_channel_stats_provider_model_timestamp_success "
    "ON channel_stats(provider, model, timestamp, success)"
)
# 修改原因：渠道 API key 排名按 provider 和时间范围过滤，再按 provider_api_key 聚合并连接 request_id。
# 修改方式：增加 provider、timestamp、provider_api_key、success、request_id 覆盖索引。
# 目的：让成功率统计和 token 连接先在小范围渠道索引内筛选，避免扫描整个 channel_stats。
CHANNEL_STATS_PROVIDER_TIMESTAMP_KEY_SUCCESS_REQUEST_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_channel_stats_provider_timestamp_key_success_request "
    "ON channel_stats(provider, timestamp, provider_api_key, success, request_id)"
)
# 修改原因：D1 初始化和 SQLite 启动迁移需要共享同一组性能索引，避免两条数据库路径优化不一致。
# 修改方式：将 Dashboard、日志列表和渠道分析所需的复合索引集中成不可变元组。
# 目的：新增实例、旧 SQLite 库重启迁移、D1 初始化都能自动获得相同的查询计划基础。
STATS_PERFORMANCE_INDEX_SQLS = (
    REQUEST_STATS_TS_PROVIDER_MODEL_INDEX_SQL,
    REQUEST_STATS_MODEL_TIMESTAMP_INDEX_SQL,
    REQUEST_STATS_REQUEST_ID_TOKEN_INDEX_SQL,
    CHANNEL_STATS_PROVIDER_MODEL_TIMESTAMP_SUCCESS_INDEX_SQL,
    CHANNEL_STATS_PROVIDER_TIMESTAMP_KEY_SUCCESS_REQUEST_INDEX_SQL,
)
# 修改原因：D1 的 CREATE TABLE IF NOT EXISTS 不会给旧表补列，而新增上游响应头后 D1 写入会包含该列。
# 修改方式：为 D1 启动迁移单独维护新增的文本日志列集合。
# 目的：确保旧 D1 表在服务启动时补齐 upstream_response_headers，避免插入日志时报缺列。
D1_TEXT_STAT_COLUMNS = {"upstream_response_headers"}

is_debug = env_bool("DEBUG", False)

# 根据数据库类型，动态创建信号量
# - SQLite 需要严格的串行写入
# - Postgres / TiDB(MySQL) 可处理更高并发
# - D1 走 HTTP API，适当并发即可
# 这里使用 db.py 的解析结果（支持 DATABASE_URL 自动识别）
_db_type = (DB_TYPE or "sqlite").lower()
if _db_type == "sqlite":
    db_semaphore = Semaphore(1)
    logger.info("Database semaphore configured for SQLite (1 concurrent writer).")
elif _db_type == "d1":
    db_semaphore = Semaphore(20)
    logger.info("Database semaphore configured for D1 (20 concurrent writers).")
else:
    # 允许 50 个并发写入操作（适用于 Postgres / TiDB(MySQL) 等）
    db_semaphore = Semaphore(50)
    if _db_type == "mysql":
        logger.info("Database semaphore configured for TiDB/MySQL (50 concurrent writers).")
    else:
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


async def _create_tables_d1():
    """D1 模式下创建表结构（SQLite 兼容 SQL）。"""

    from db import d1_client
    if d1_client is None:
        raise RuntimeError("D1 client is not initialized")

    create_sqls = [
        """
        CREATE TABLE IF NOT EXISTS request_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            endpoint TEXT,
            client_ip TEXT,
            process_time REAL,
            first_response_time REAL,
            content_start_time REAL,
            provider TEXT,
            model TEXT,
            api_key TEXT,
            success INTEGER DEFAULT 0,
            status_code INTEGER,
            is_flagged INTEGER DEFAULT 0,
            text TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cached_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            prompt_price REAL DEFAULT 0.0,
            completion_price REAL DEFAULT 0.0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            provider_id TEXT,
            provider_key_index INTEGER,
            api_key_name TEXT,
            api_key_group TEXT,
            retry_count INTEGER DEFAULT 0,
            retry_path TEXT,
            request_headers TEXT,
            request_body TEXT,
            upstream_request_headers TEXT,
            upstream_request_body TEXT,
            upstream_response_headers TEXT,
            upstream_response_body TEXT,
            response_body TEXT,
            raw_data_expires_at DATETIME
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS channel_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT,
            provider TEXT,
            model TEXT,
            api_key TEXT,
            provider_api_key TEXT,
            success INTEGER DEFAULT 0,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS admin_user (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            jwt_secret TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS app_config (
            id INTEGER PRIMARY KEY,
            config_json TEXT,
            config_yaml TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """,
    ]

    index_sqls = [
        "CREATE INDEX IF NOT EXISTS idx_request_stats_provider ON request_stats(provider)",
        "CREATE INDEX IF NOT EXISTS idx_request_stats_model ON request_stats(model)",
        "CREATE INDEX IF NOT EXISTS idx_request_stats_api_key ON request_stats(api_key)",
        "CREATE INDEX IF NOT EXISTS idx_request_stats_success ON request_stats(success)",
        "CREATE INDEX IF NOT EXISTS idx_request_stats_status_code ON request_stats(status_code)",
        "CREATE INDEX IF NOT EXISTS idx_request_stats_timestamp ON request_stats(timestamp)",
        *STATS_PERFORMANCE_INDEX_SQLS,
        "CREATE INDEX IF NOT EXISTS idx_channel_stats_provider ON channel_stats(provider)",
        "CREATE INDEX IF NOT EXISTS idx_channel_stats_model ON channel_stats(model)",
        "CREATE INDEX IF NOT EXISTS idx_channel_stats_provider_api_key ON channel_stats(provider_api_key)",
        "CREATE INDEX IF NOT EXISTS idx_channel_stats_timestamp ON channel_stats(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_channel_stats_cover ON channel_stats(timestamp, provider, model, success)",
        "CREATE INDEX IF NOT EXISTS idx_admin_user_username ON admin_user(username)",
        "CREATE INDEX IF NOT EXISTS idx_app_config_updated_at ON app_config(updated_at)",
    ]

    for sql in create_sqls + index_sqls:
        await d1_client.execute(sql)

    # D1 的 CREATE TABLE IF NOT EXISTS 不会给旧表补列；这里按启动迁移补齐 Prompt Caching 字段。
    existing_columns = {
        row.get("name")
        for row in await d1_client.query_all("PRAGMA table_info(request_stats)")
        if row.get("name")
    }
    for column_name in PROMPT_CACHE_STAT_COLUMNS:
        if column_name not in existing_columns:
            await d1_client.execute(
                f"ALTER TABLE request_stats ADD COLUMN {column_name} INTEGER DEFAULT 0"
            )
            existing_columns.add(column_name)
            logger.info("Added D1 request_stats column '%s' for Prompt Caching stats.", column_name)

    for column_name in D1_TEXT_STAT_COLUMNS:
        if column_name not in existing_columns:
            # 修改原因：旧 D1 表不会自动拥有新加入的上游响应头列。
            # 修改方式：启动时用 ALTER TABLE ADD COLUMN 补齐 TEXT 列。
            # 目的：保证 D1 模式下日志插入、查询和清理都能使用 upstream_response_headers。
            await d1_client.execute(
                f"ALTER TABLE request_stats ADD COLUMN {column_name} TEXT"
            )
            existing_columns.add(column_name)
            logger.info("Added D1 request_stats text column '%s'.", column_name)


async def create_tables():
    """创建数据库表并执行简易列迁移"""
    if DISABLE_DATABASE:
        return
    if (DB_TYPE or "sqlite").lower() == "d1":
        await _create_tables_d1()
        return

    async with db_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # SQLite: 启用 incremental auto_vacuum，DELETE/UPDATE 后自动回收磁盘空间
        db_type = (DB_TYPE or "sqlite").lower()
        if db_type == "sqlite":
            current_av = (await conn.execute(text("PRAGMA auto_vacuum"))).scalar()
            if current_av != 2:
                await conn.execute(text("PRAGMA auto_vacuum = INCREMENTAL"))
                # auto_vacuum 切换需要 VACUUM 生效
                raw_conn = await conn.get_raw_connection()
                raw = raw_conn.dbapi_connection
                if hasattr(raw, '_connection'):
                    # aiosqlite wraps the real connection
                    await raw._connection.execute("VACUUM")
                else:
                    raw.execute("VACUUM")
                logger.info("SQLite auto_vacuum set to INCREMENTAL")

        # 检查并添加缺失的列 - 扩展此简易迁移以支持 SQLite / PostgreSQL / TiDB(MySQL)
        db_type = (DB_TYPE or "sqlite").lower()
        if db_type in ["sqlite", "postgres", "mysql", "d1"]:
            def check_and_add_columns(connection):
                inspector = inspect(connection)
                preparer = connection.dialect.identifier_preparer
                for table in [RequestStat, ChannelStat, AppConfig, AdminUser]:
                    table_name = table.__tablename__
                    existing_columns = {col['name'] for col in inspector.get_columns(table_name)}

                    for column_name, column in table.__table__.columns.items():
                        if column_name not in existing_columns:
                            # 适配 PostgreSQL / SQLite 的类型映射
                            # 注意：JSON/JSONB 在不同方言下 compile 结果不同，
                            # 且 CockroachDB 对 JSONB 的兼容也可能返回 JSON。
                            col_type = column.type.compile(connection.dialect)

                            # Prompt Caching 字段需要跨数据库保持 DEFAULT 0；其它列沿用原有保守策略以避免 JSON 默认值兼容问题。
                            if table_name == "request_stats" and column_name in PROMPT_CACHE_STAT_COLUMNS:
                                default = " DEFAULT 0"
                            else:
                                # SQLite 允许 DEFAULT；Postgres/Cockroach 对 JSON 默认值较敏感，这里统一不加默认
                                default = _get_default_sql(column.default) if db_type == "sqlite" else ""

                            # 使用标准的 ALTER TABLE 语法
                            qt = preparer.quote(table_name)
                            qc = preparer.quote(column_name)
                            connection.execute(
                                text(
                                    f'ALTER TABLE {qt} ADD COLUMN {qc} {col_type}{default}'
                                )
                            )
                            logger.info(
                                f"Added column '{column_name}' ({col_type}) to table '{table_name}'."
                            )

            await conn.run_sync(check_and_add_columns)

            if db_type == "sqlite":
                # 修改原因：SQLAlchemy 模型历史上只创建单列索引，旧 SQLite 库不会自动拥有本次新增复合索引。
                # 修改方式：启动迁移阶段逐条执行 STATS_PERFORMANCE_INDEX_SQLS，与 D1 初始化列表保持一致。
                # 目的：让新部署和重启后的 SQLite 实例自动获得 Dashboard、日志列表和渠道分析所需索引。
                for index_sql in STATS_PERFORMANCE_INDEX_SQLS:
                    await conn.execute(text(index_sql))

            # MySQL 专属：将 body 列从 TEXT (64KB) 升级到 MEDIUMTEXT (16MB)
            # v1.4.1 起默认保存请求/响应体，截断上限 100KB 超出 TEXT 容量
            if db_type == "mysql":
                _body_columns = [
                    ("request_stats", "request_body"),
                    ("request_stats", "upstream_request_body"),
                    ("request_stats", "upstream_response_body"),
                    ("request_stats", "response_body"),
                ]

                def _upgrade_text_to_mediumtext(connection):
                    insp = inspect(connection)
                    for tbl, col in _body_columns:
                        for col_info in insp.get_columns(tbl):
                            if col_info["name"] == col:
                                col_type_str = str(col_info["type"]).upper()
                                if col_type_str == "TEXT":
                                    connection.execute(
                                        text(
                                            f"ALTER TABLE `{tbl}` MODIFY COLUMN `{col}` MEDIUMTEXT"
                                        )
                                    )
                                    logger.info(
                                        f"Upgraded column '{col}' in '{tbl}' from TEXT to MEDIUMTEXT."
                                    )
                                break

                await conn.run_sync(_upgrade_text_to_mediumtext)


# ============== 成本计算 ==============

def _parse_price_str(price_str) -> tuple:
    """解析 '输入,输出,缓存输入' 格式的价格字符串。"""
    # 修改原因：缓存命中 token 需要使用单独价格，但现有数据库只保存 prompt_price 与 completion_price。
    # 修改方式：配置价格支持第三段 cached_price；缺省第三段时回退到 prompt_price，保持旧配置兼容。
    # 目的：让统计写入阶段能够把缓存折扣折算进 prompt_price，而不改数据库结构和查询公式。
    parts = [p.strip() for p in str(price_str).split(",")]
    try:
        prompt_price = float(parts[0]) if len(parts) > 0 and parts[0] != "" else 0.0
        completion_price = float(parts[1]) if len(parts) > 1 and parts[1] != "" else 0.0
        cached_price = float(parts[2]) if len(parts) > 2 and parts[2] != "" else prompt_price
    except (ValueError, TypeError):
        return None
    return prompt_price, completion_price, cached_price


def _match_model_price(model_price_dict: dict, model_name: str, *, use_default: bool = True):
    """
    在一个 model_price 字典中，按前缀匹配模型名，返回 (prompt_price, completion_price, cached_price) 或 None。

    匹配规则：遍历字典 key，如果 model_name 以该 key 开头则命中；
    多个前缀同时匹配时，取最长的那个（最精确匹配）。
    use_default=True 时，未命中前缀也会尝试 "default" 兜底。
    """
    if not model_price_dict or not model_name:
        return None
    # 前缀匹配：收集所有命中的 key，取最长的（最精确）
    matched = [(k, model_price_dict[k]) for k in model_price_dict if k and k != "default" and model_name.startswith(k)]
    if matched:
        matched.sort(key=lambda x: len(x[0]), reverse=True)
        return _parse_price_str(matched[0][1])
    # 兜底 default
    if use_default:
        default_str = model_price_dict.get("default")
        if default_str is not None:
            return _parse_price_str(default_str)
    return None


def get_current_model_prices(app, model_name: str, provider_name: str = None):
    """
    根据配置返回指定模型的 prompt_price、completion_price 和 cached_price（单位：$/M tokens）。

    查找优先级：
    1. 渠道级 provider.preferences.model_price（前缀匹配）
    2. 全局 preferences.model_price（前缀匹配）
    3. 都未配置 → 返回 (0, 0, 0)，即不计费

    Args:
        app: FastAPI 应用实例
        model_name: 模型名称
        provider_name: 渠道名称（可选）

    Returns:
        (prompt_price, completion_price, cached_price) 元组
    """
    from utils import safe_get
    try:
        provider_prices = {}
        global_prices = safe_get(app.state.config, 'preferences', 'model_price', default={})

        # 1. 渠道级：去 model_prefix + 精确/前缀匹配（不用 default）
        if provider_name:
            providers = safe_get(app.state.config, 'providers', default=[])
            for p in providers:
                if p.get('provider') == provider_name:
                    # 去掉渠道 model_prefix（如 [eve]claude-sonnet-4.5 → claude-sonnet-4.5）
                    prefix = (p.get('model_prefix') or '').strip()
                    if prefix and model_name.startswith(prefix):
                        model_name = model_name[len(prefix):]
                    provider_prices = safe_get(p, 'preferences', 'model_price', default={})
                    result = _match_model_price(provider_prices, model_name, use_default=False)
                    if result is not None:
                        return result
                    break

        # 2. 全局精确/前缀匹配（不用 default）
        result = _match_model_price(global_prices, model_name, use_default=False)
        if result is not None:
            return result

        # 3. 外部价格库 fallback
        try:
            from .default_prices import lookup_price
            result = lookup_price(model_name)
            if result is not None:
                return result
        except Exception:
            pass

        # 4. 渠道 default 兜底
        if provider_prices.get("default") is not None:
            result = _parse_price_str(provider_prices["default"])
            if result is not None:
                return result

        # 5. 全局 default 兜底
        if global_prices.get("default") is not None:
            result = _parse_price_str(global_prices["default"])
            if result is not None:
                return result

        # 6. 都未配置，不计费
        return 0.0, 0.0, 0.0
    except Exception:
        return 0.0, 0.0, 0.0


async def compute_total_cost_from_db(filter_api_key: Optional[str] = None, start_dt_obj: Optional[datetime] = None) -> float:
    """
    直接从数据库历史记录累计成本：
    sum((prompt_tokens*prompt_price + completion_tokens*completion_price)/1e6)
    """
    if DISABLE_DATABASE:
        return 0.0

    if (DB_TYPE or "sqlite").lower() == "d1":
        from db import d1_client
        if d1_client is None:
            return 0.0

        sql = (
            "SELECT COALESCE(SUM((COALESCE(prompt_tokens, 0) * COALESCE(prompt_price, 0.0) "
            "+ COALESCE(completion_tokens, 0) * COALESCE(completion_price, 0.0)) / 1000000.0), 0.0) AS total_cost "
            "FROM request_stats WHERE 1=1"
        )
        params: list[Any] = []
        if filter_api_key:
            sql += " AND api_key = ?"
            params.append(filter_api_key)
        if start_dt_obj:
            sql += " AND timestamp >= ?"
            params.append(format_d1_datetime(start_dt_obj))
        total_cost = await d1_client.query_value(sql, params, column="total_cost", default=0.0)
        try:
            return float(total_cost or 0.0)
        except Exception:
            return 0.0

    async with async_session_scope() as session:
        expr = (func.coalesce(RequestStat.prompt_tokens, 0) * func.coalesce(RequestStat.prompt_price, 0.0) + func.coalesce(RequestStat.completion_tokens, 0) * func.coalesce(RequestStat.completion_price, 0.0)) / 1000000.0
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


async def _query_token_usage_d1(
    filter_api_key: Optional[str] = None,
    filter_model: Optional[str] = None,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict]:
    from db import d1_client
    if d1_client is None:
        return []

    sql = (
        "SELECT api_key, model, "
        "COALESCE(SUM(prompt_tokens), 0) AS total_prompt_tokens, "
        "COALESCE(SUM(completion_tokens), 0) AS total_completion_tokens, "
        "COALESCE(SUM(total_tokens), 0) AS total_tokens, "
        "COUNT(id) AS request_count "
        "FROM request_stats WHERE 1=1"
    )
    params: list[Any] = []
    if filter_api_key:
        sql += " AND api_key = ?"
        params.append(filter_api_key)
    if filter_model:
        sql += " AND model = ?"
        params.append(filter_model)
    if start_dt:
        sql += " AND timestamp >= ?"
        params.append(format_d1_datetime(start_dt))
    if end_dt:
        sql += " AND timestamp < ?"
        params.append(format_d1_datetime(end_dt + timedelta(days=1)))
    if not filter_model:
        sql += " AND model IS NOT NULL AND model != ''"
    sql += " GROUP BY api_key, model"

    rows = await d1_client.query_all(sql, params)
    processed_usage = []
    for row in rows:
        api_key = row.get("api_key", "")
        if api_key and len(api_key) > 7:
            api_key_prefix = f"{api_key[:7]}...{api_key[-4:]}"
        else:
            api_key_prefix = api_key
        processed_usage.append(
            {
                "api_key_prefix": api_key_prefix,
                "model": row.get("model"),
                "total_prompt_tokens": int(row.get("total_prompt_tokens") or 0),
                "total_completion_tokens": int(row.get("total_completion_tokens") or 0),
                "total_tokens": int(row.get("total_tokens") or 0),
                "request_count": int(row.get("request_count") or 0),
            }
        )
    return processed_usage


# ============== 统计写入 ==============


def _unpack_model_prices(price_result) -> tuple[float, float, float]:
    """
    修改原因：价格接口新增 cached_price，但部分注入的 get_model_prices_func 可能仍返回旧的二元组。
    修改方式：统一把二元组补齐为三元组，第三段缺省时使用 prompt_price。
    目的：让 enqueue_stats 和 update_stats 共用兼容逻辑，避免不同写入路径出现解包错误。
    """
    if len(price_result) == 3:
        return price_result
    prompt_price, completion_price = price_result
    return prompt_price, completion_price, prompt_price


def _apply_cached_prompt_price(current_info: dict, prompt_price: float, cached_price: float) -> None:
    """
    修改原因：数据库没有单独的 cached_price 字段，不能通过表结构保存缓存输入价。
    修改方式：按 cached_tokens 对 prompt_price 做加权折算，再覆盖 current_info["prompt_price"]。
    目的：沿用现有成本公式，同时让缓存命中 token 按缓存价格计费。
    """
    cached_tokens = current_info.get("cached_tokens", 0) or 0
    prompt_tokens = current_info.get("prompt_tokens", 0) or 0
    if cached_tokens > 0 and prompt_tokens > 0 and cached_price != prompt_price:
        # clamp: 防止异常情况 cached_tokens > prompt_tokens
        billable_cached = min(cached_tokens, prompt_tokens)
        non_cached = prompt_tokens - billable_cached
        effective = (non_cached * prompt_price + billable_cached * cached_price) / prompt_tokens
        current_info["prompt_price"] = round(effective, 10)


def enqueue_stats(current_info: dict, app=None, get_model_prices_func=None) -> None:
    """将请求统计放入 buffer，由常驻 consumer 批量写入。"""
    # 修改原因：请求路径不能再直接等待 SQLite 写入，否则会在 db_semaphore 前堆积协程并阻塞事件循环。
    # 修改方式：同步计算价格后只保存 current_info.copy() 快照，再启动或唤醒唯一的后台 consumer。
    # 目的：让请求结束路径保持轻量，同时防止调用方后续修改 current_info 影响待写入统计。
    if DISABLE_DATABASE:
        return

    try:
        if current_info.get("success") and current_info.get("model"):
            if get_model_prices_func:
                price_result = get_model_prices_func(current_info["model"])
            elif app:
                price_result = get_current_model_prices(
                    app, current_info["model"], provider_name=current_info.get("provider"))
            else:
                price_result = 0.0, 0.0, 0.0
            prompt_price, completion_price, cached_price = _unpack_model_prices(price_result)
            current_info["prompt_price"] = prompt_price
            current_info["completion_price"] = completion_price
            _apply_cached_prompt_price(current_info, prompt_price, cached_price)
    except Exception:
        pass

    _stats_buffer.append((current_info.copy(), app))
    _ensure_stats_consumer_started()
    if len(_stats_buffer) >= _STATS_BATCH_SIZE and _stats_flush_event is not None:
        _stats_flush_event.set()


def _ensure_stats_consumer_started() -> None:
    """确保 request stats 常驻 consumer 已经启动。"""
    global _stats_consumer_started, _stats_flush_event
    # 修改原因：enqueue_stats 是同步入口，不能 await 后台任务，也不能为每条统计创建一个 task。
    # 修改方式：在当前事件循环上创建一个常驻 consumer，并复用同一个 asyncio.Event 作为批量唤醒信号。
    # 目的：保持请求路径无等待，且在启动或关闭阶段没有 running loop 时安全返回。
    if _stats_consumer_started:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _stats_flush_event = asyncio.Event()
    loop.create_task(_stats_consumer())
    _stats_consumer_started = True


async def _stats_consumer() -> None:
    """常驻后台任务：每 2 秒或攒满 20 条时批量写入 request_stats。"""
    global _stats_consumer_started, _stats_flush_event
    # 修改原因：SQLite 写入需要串行化，逐请求协程在锁等待时会造成内存和事件循环压力。
    # 修改方式：consumer 按固定批量从 deque 取出请求统计，并调用 _batch_write_stats 在一个事务内写入。
    # 目的：把后台协程数量固定为一个，减少事务次数，并在任务取消时尽量 flush 剩余统计。
    try:
        while True:
            try:
                if _stats_flush_event is None:
                    await asyncio.sleep(_STATS_FLUSH_INTERVAL)
                else:
                    await asyncio.wait_for(
                        _stats_flush_event.wait(),
                        timeout=_STATS_FLUSH_INTERVAL,
                    )
                    _stats_flush_event.clear()
            except asyncio.TimeoutError:
                pass

            if not _stats_buffer:
                continue

            batch = []
            while _stats_buffer and len(batch) < _STATS_BATCH_SIZE:
                batch.append(_stats_buffer.popleft())

            await _batch_write_stats(batch)
    except asyncio.CancelledError:
        while _stats_buffer:
            batch = []
            while _stats_buffer and len(batch) < _STATS_BATCH_SIZE:
                batch.append(_stats_buffer.popleft())
            try:
                await _batch_write_stats(batch)
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Stats consumer crashed: {e}")
    finally:
        _stats_consumer_started = False
        _stats_flush_event = None


async def _batch_write_stats(batch: list) -> None:
    """批量写入 request_stats，一个事务多条 INSERT。"""
    # 修改原因：逐条调用 update_stats 会让 SQLite 每条请求统计都开启一次事务，锁等待时会放大后台积压。
    # 修改方式：consumer 传入一批 current_info 快照，SQLAlchemy 路径用一个 session 事务，D1 路径在同一信号量内逐条写入。
    # 目的：减少事务数量、fsync 次数和信号量等待协程数量，同时保持 request_stats 表结构和字段清洗规则不变。
    if not batch:
        return

    for attempt in range(SQLITE_MAX_RETRIES):
        try:
            if (DB_TYPE or "sqlite").lower() == "d1":
                from db import d1_client
                if d1_client is None:
                    return
                async with db_semaphore:
                    for current_info, app in batch:
                        columns = [column.key for column in RequestStat.__table__.columns]
                        filtered_info = {k: v for k, v in current_info.items() if k in columns}
                        for key, value in list(filtered_info.items()):
                            if isinstance(value, str):
                                filtered_info[key] = value.replace('\x00', '')
                            elif isinstance(value, bool):
                                filtered_info[key] = 1 if value else 0
                            elif isinstance(value, datetime):
                                filtered_info[key] = format_d1_datetime(value)

                        insert_cols = [k for k in filtered_info.keys() if k != "id"]
                        placeholders = ", ".join(["?" for _ in insert_cols])
                        sql = (
                            f"INSERT INTO request_stats ({', '.join(insert_cols)}) "
                            f"VALUES ({placeholders})"
                        )
                        params = [filtered_info[k] for k in insert_cols]
                        await d1_client.execute(sql, params)

                for current_info, app in batch:
                    check_key = current_info.get("api_key")
                    if app and check_key and hasattr(app.state, 'paid_api_keys_states'):
                        if check_key in app.state.paid_api_keys_states and current_info.get("total_tokens", 0) > 0:
                            await update_paid_api_keys_states(app, check_key)
                return

            async with db_semaphore:
                async with async_session_scope() as session:
                    async with session.begin():
                        for current_info, app in batch:
                            columns = [column.key for column in RequestStat.__table__.columns]
                            filtered_info = {k: v for k, v in current_info.items() if k in columns}

                            # 修改原因：request_stats 仍可能保存上游原始文本，NUL 字符会导致部分数据库写入失败。
                            # 修改方式：批量写入前沿用 update_stats 的字段过滤和字符串清洗规则。
                            # 目的：只改变写入调度方式，不改变数据库中可接受的数据形态。
                            for key, value in filtered_info.items():
                                if isinstance(value, str):
                                    filtered_info[key] = value.replace('\x00', '')

                            new_request_stat = RequestStat(**filtered_info)
                            session.add(new_request_stat)

            for current_info, app in batch:
                check_key = current_info.get("api_key")
                if app and check_key and hasattr(app.state, 'paid_api_keys_states'):
                    if check_key in app.state.paid_api_keys_states and current_info.get("total_tokens", 0) > 0:
                        await update_paid_api_keys_states(app, check_key)
            return

        except Exception as e:
            error_str = str(e).lower()
            is_lock_error = 'database is locked' in error_str or 'busy' in error_str

            if is_lock_error and attempt < SQLITE_MAX_RETRIES - 1:
                delay = SQLITE_RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Database locked (batch stats), retrying in {delay}s (attempt {attempt + 1}/{SQLITE_MAX_RETRIES})")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Error batch updating stats: {str(e)}")
                if is_debug:
                    import traceback
                    traceback.print_exc()
                break


async def update_stats(current_info: dict, app=None, get_model_prices_func=None):
    """
    更新请求统计到数据库
    
    Args:
        current_info: 包含请求信息的字典
        app: FastAPI 应用实例（用于获取模型价格）
        get_model_prices_func: 获取模型价格的函数，签名为 (model_name) -> (prompt_price, completion_price[, cached_price])
    """
    if DISABLE_DATABASE:
        return

    # 在成功请求时，快照当前价格，写入数据库
    try:
        if current_info.get("success") and current_info.get("model"):
            if get_model_prices_func:
                price_result = get_model_prices_func(current_info["model"])
            elif app:
                price_result = get_current_model_prices(
                    app, current_info["model"], provider_name=current_info.get("provider"))
            else:
                price_result = 0.0, 0.0, 0.0
            prompt_price, completion_price, cached_price = _unpack_model_prices(price_result)
            current_info["prompt_price"] = prompt_price
            current_info["completion_price"] = completion_price
            _apply_cached_prompt_price(current_info, prompt_price, cached_price)
    except Exception:
        pass

    # 使用重试机制写入数据库
    for attempt in range(SQLITE_MAX_RETRIES):
        try:
            if (DB_TYPE or "sqlite").lower() == "d1":
                from db import d1_client
                if d1_client is None:
                    return
                async with db_semaphore:
                    columns = [column.key for column in RequestStat.__table__.columns]
                    filtered_info = {k: v for k, v in current_info.items() if k in columns}
                    for key, value in list(filtered_info.items()):
                        if isinstance(value, str):
                            filtered_info[key] = value.replace('\x00', '')
                        elif isinstance(value, bool):
                            filtered_info[key] = 1 if value else 0
                        elif isinstance(value, datetime):
                            filtered_info[key] = format_d1_datetime(value)

                    insert_cols = [k for k in filtered_info.keys() if k != "id"]
                    placeholders = ", ".join(["?" for _ in insert_cols])
                    sql = (
                        f"INSERT INTO request_stats ({', '.join(insert_cols)}) "
                        f"VALUES ({placeholders})"
                    )
                    params = [filtered_info[k] for k in insert_cols]
                    await d1_client.execute(sql, params)

                check_key = current_info.get("api_key")
                if app and check_key and hasattr(app.state, 'paid_api_keys_states'):
                    if check_key in app.state.paid_api_keys_states and current_info.get("total_tokens", 0) > 0:
                        await update_paid_api_keys_states(app, check_key)
                return

            # 等待获取数据库访问权限
            async with db_semaphore:
                async with async_session_scope() as session:
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
            if (DB_TYPE or "sqlite").lower() == "d1":
                from db import d1_client
                if d1_client is None:
                    return
                async with db_semaphore:
                    sql = (
                        "INSERT INTO channel_stats "
                        "(request_id, provider, model, api_key, provider_api_key, success, timestamp) "
                        "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)"
                    )
                    params = [
                        request_id,
                        provider,
                        model,
                        api_key,
                        provider_api_key,
                        1 if success else 0,
                    ]
                    await d1_client.execute(sql, params)
                return

            async with db_semaphore:
                async with async_session_scope() as session:
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


async def batch_update_channel_stats(items: list) -> None:
    """批量写入 channel stats，一个事务多条 INSERT。"""
    # 修改原因：逐条调用 update_channel_stats 会让 SQLite 每条统计都开启一次事务，锁等待时也会放大后台积压。
    # 修改方式：把 consumer 传入的一批统计合并写入，SQLAlchemy 路径使用一个 session 事务，D1 路径优先使用多 VALUES INSERT。
    # 目的：把常见场景下 50 条统计的提交次数从 50 次降到 1 次，降低 SQLite fsync 和锁竞争成本。
    if DISABLE_DATABASE or not items:
        return

    for attempt in range(SQLITE_MAX_RETRIES):
        try:
            if (DB_TYPE or "sqlite").lower() == "d1":
                from db import d1_client
                if d1_client is None:
                    return

                # 修改原因：D1 走 HTTP API，逐条请求会产生额外网络往返；但当前 D1 客户端没有专门 batch 方法。
                # 修改方式：优先拼接 SQLite 兼容的多 VALUES INSERT；如果当前 D1 环境不支持，再回退到原逐条函数。
                # 目的：让 D1 路径也能在支持时批量写入，并在不支持时保持统计不丢失。
                placeholders = ", ".join(
                    ["(?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)" for _ in items]
                )
                sql = (
                    "INSERT INTO channel_stats "
                    "(request_id, provider, model, api_key, provider_api_key, success, timestamp) "
                    f"VALUES {placeholders}"
                )
                params = []
                for item in items:
                    params.extend([
                        item["request_id"],
                        item["provider"],
                        item["model"],
                        item["api_key"],
                        item.get("provider_api_key"),
                        1 if item["success"] else 0,
                    ])

                fallback_to_single = False
                async with db_semaphore:
                    try:
                        await d1_client.execute(sql, params)
                    except Exception as batch_exc:
                        error_str = str(batch_exc).lower()
                        is_lock_error = 'database is locked' in error_str or 'busy' in error_str
                        if is_lock_error:
                            raise
                        logger.warning(f"D1 batch channel stats insert failed, falling back to single inserts: {batch_exc}")
                        fallback_to_single = True

                if fallback_to_single:
                    # 修改原因：update_channel_stats 内部也会获取 db_semaphore，不能在持有信号量时回退调用它。
                    # 修改方式：先退出 D1 批量写入的 semaphore 作用域，再逐条调用原函数。
                    # 目的：避免 D1 批量不支持时发生自我等待，同时保持原有逐条重试逻辑。
                    for item in items:
                        await update_channel_stats(
                            item["request_id"],
                            item["provider"],
                            item["model"],
                            item["api_key"],
                            item["success"],
                            provider_api_key=item.get("provider_api_key"),
                        )
                return

            async with db_semaphore:
                async with async_session_scope() as session:
                    async with session.begin():
                        # 修改原因：ChannelStat 对象仍需交给 SQLAlchemy ORM 生成，以保持字段默认值和现有模型一致。
                        # 修改方式：一次构造本批次的 ORM 对象并 add_all，在同一个事务上下文内提交。
                        # 目的：保持旧数据结构不变，只减少事务数量和提交次数。
                        channel_stats = [
                            ChannelStat(
                                request_id=item["request_id"],
                                provider=item["provider"],
                                model=item["model"],
                                api_key=item["api_key"],
                                provider_api_key=item.get("provider_api_key"),
                                success=item["success"],
                            )
                            for item in items
                        ]
                        session.add_all(channel_stats)
            return

        except Exception as e:
            error_str = str(e).lower()
            is_lock_error = 'database is locked' in error_str or 'busy' in error_str

            if is_lock_error and attempt < SQLITE_MAX_RETRIES - 1:
                delay = SQLITE_RETRY_DELAY * (2 ** attempt)
                logger.warning(f"Database locked (batch channel stats), retrying in {delay}s (attempt {attempt + 1}/{SQLITE_MAX_RETRIES})")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Error batch updating channel stats: {str(e)}")
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
        # Mask API key (show prefix like zk-...xyz)
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
    if (DB_TYPE or "sqlite").lower() == "d1":
        usage_data = await _query_token_usage_d1(
            filter_api_key=filter_api_key,
            filter_model=filter_model,
            start_dt=start_dt_obj,
            end_dt=end_dt_obj,
        )
    else:
        async with async_session_scope() as session:
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