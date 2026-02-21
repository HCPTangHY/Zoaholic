import os
import ssl as ssl_module
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import ipaddress
from typing import Optional

from sqlalchemy import event, text

from core.env import env_bool
from core.d1_client import D1HTTPClient
from sqlalchemy.sql import func

# --- CockroachDB compatibility (asyncpg / SQLAlchemy) ---
# CockroachDB 并不一定提供 pg_catalog.json 类型（通常只有 jsonb）。
# SQLAlchemy 的 postgresql+asyncpg 方言会在连接时尝试注册 json/jsonb codec，
# 在 CockroachDB 上可能报错：ValueError: unknown type: pg_catalog.json
# 这里对 SQLAlchemy 的 codec 注册逻辑做一个兼容补丁：若 json 类型不存在，则仅注册 jsonb。
try:
    import json as _json_module
    from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg as _PGDialect_asyncpg

    if not getattr(_PGDialect_asyncpg, "_zoaholic_crdb_json_patch", False):
        _orig_setup = _PGDialect_asyncpg.setup_asyncpg_json_codec

        async def _patched_setup_asyncpg_json_codec(self, asyncpg_connection, *args, **kwargs):
            try:
                return await _orig_setup(self, asyncpg_connection, *args, **kwargs)
            except ValueError as e:
                msg = str(e)
                if "unknown type: pg_catalog.json" not in msg:
                    raise

                # CockroachDB 兼容：没有 pg_catalog.json 时，直接跳过 SQLAlchemy 的 json codec 注册。
                # asyncpg 对 jsonb 通常仍能使用默认 codec；且我们业务侧也可容忍 json 以文本形式返回。
                return None
            except AttributeError:
                # 某些 SQLAlchemy/asyncpg 组合下传入的连接适配器不暴露 set_type_codec 等方法。
                # 直接跳过 codec 注册即可。
                return None

        _PGDialect_asyncpg.setup_asyncpg_json_codec = _patched_setup_asyncpg_json_codec
        _PGDialect_asyncpg._zoaholic_crdb_json_patch = True
except Exception:
    # 兼容：未安装 sqlalchemy/asyncpg 时不处理
    pass

# Render / Railway 等平台通常提供 DATABASE_URL（多为 postgres://...），这里统一解析。
DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("DB_URL")
    or os.getenv("SQLALCHEMY_DATABASE_URL")
)

# Cloudflare D1 连接参数（HTTP API）
D1_ACCOUNT_ID = (
    os.getenv("D1_ACCOUNT_ID")
    or os.getenv("CF_ACCOUNT_ID")
    or os.getenv("CLOUDFLARE_ACCOUNT_ID")
    or ""
).strip()
D1_DATABASE_ID = (
    os.getenv("D1_DATABASE_ID")
    or os.getenv("CF_D1_DATABASE_ID")
    or ""
).strip()
D1_API_TOKEN = (
    os.getenv("D1_API_TOKEN")
    or os.getenv("CF_API_TOKEN")
    or os.getenv("CLOUDFLARE_API_TOKEN")
    or ""
).strip()
D1_API_BASE_URL = (os.getenv("D1_API_BASE_URL") or "https://api.cloudflare.com/client/v4").strip()
D1_TIMEOUT_SECONDS = float(os.getenv("D1_TIMEOUT_SECONDS", "30"))

# DB_TYPE：显式优先；否则根据 DATABASE_URL 自动推断；默认 sqlite
_DB_TYPE_ENV = (os.getenv("DB_TYPE") or "").strip().lower()
if _DB_TYPE_ENV:
    DB_TYPE = _DB_TYPE_ENV
elif DATABASE_URL:
    _url = DATABASE_URL.strip().lower()
    if _url.startswith("postgres://") or _url.startswith("postgresql://"):
        DB_TYPE = "postgres"
    elif _url.startswith("mysql://") or _url.startswith("mariadb://"):
        DB_TYPE = "mysql"
    elif _url.startswith("mysql+") or _url.startswith("mariadb+"):
        # 例如 mysql+asyncmy://... / mysql+pymysql://...
        DB_TYPE = "mysql"
    elif _url.startswith("sqlite://"):
        DB_TYPE = "sqlite"
    elif _url.startswith("d1://"):
        DB_TYPE = "d1"
    else:
        DB_TYPE = "sqlite"
elif D1_ACCOUNT_ID and D1_DATABASE_ID and D1_API_TOKEN:
    DB_TYPE = "d1"
else:
    DB_TYPE = "sqlite"


def _normalize_database_url(url: str, db_type: str) -> str:
    """将常见 DATABASE_URL 规范为 SQLAlchemy async URL。"""

    url = url.strip()
    db_type = (db_type or "").lower()

    if db_type == "postgres":
        # Render: postgres://user:pass@host:port/db
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        # 常见: postgresql://...
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            return "postgresql+asyncpg://" + url[len("postgresql://") :]
        return url

    if db_type == "sqlite":
        # 常见: sqlite:///./data/stats.db
        if url.startswith("sqlite://") and "+aiosqlite" not in url:
            return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return url

    if db_type == "mysql":
        # TiDB / MySQL：统一使用 asyncmy 异步驱动，避免落到同步驱动（如 MySQLdb/mysqlclient）
        # 常见: mysql://user:pass@host:port/db
        if url.startswith("mysql://"):
            return "mysql+asyncmy://" + url[len("mysql://") :]
        if url.startswith("mariadb://"):
            return "mariadb+asyncmy://" + url[len("mariadb://") :]

        # 若用户显式写了其它驱动（如 mysql+pymysql://、mysql+mysqldb://、mysql+aiomysql://），
        # 统一改写为 asyncmy，保证与本项目依赖一致。
        if url.startswith("mysql+") and not url.startswith("mysql+asyncmy://"):
            return "mysql+asyncmy://" + url.split("://", 1)[1]
        if url.startswith("mariadb+") and not url.startswith("mariadb+asyncmy://"):
            return "mariadb+asyncmy://" + url.split("://", 1)[1]
        return url

    return url


def _extract_mysql_ssl_connect_args(db_url: str) -> tuple[str, dict]:
    """从 URL query 中提取 MySQL/TiDB SSL 参数，转换为驱动可识别的 connect_args。

    背景：
    - TiDB Cloud 通常需要 TLS 连接
    - 用户可能会在 DATABASE_URL 上附带如 ssl_mode/ssl_ca 等参数
    - 我们将其转为 `connect_args={"ssl": SSLContext | dict | bool}`
    """

    parts = urlsplit(db_url)
    qsl = parse_qsl(parts.query, keep_blank_values=True)

    ssl_mode = None
    ssl_ca = None
    ssl_cert = None
    ssl_key = None
    kept: list[tuple[str, str]] = []

    for k, v in qsl:
        lk = k.lower().replace("-", "_")
        if lk in ("sslmode", "ssl_mode"):
            ssl_mode = v
        elif lk in ("ssl", "use_ssl", "usessl", "tls"):
            # 一些平台/连接串会用 ?ssl=true 这种写法
            vv = str(v).strip().lower()
            if vv in ("0", "false", "off", "disable", "disabled"):
                ssl_mode = "disabled"
            elif vv in ("1", "true", "on", "require", "required", "yes"):
                ssl_mode = ssl_mode or "required"
        elif lk in ("sslrootcert", "ssl_ca", "sslca"):
            ssl_ca = v
        elif lk in ("sslcert", "ssl_cert"):
            ssl_cert = v
        elif lk in ("sslkey", "ssl_key"):
            ssl_key = v
        else:
            kept.append((k, v))

    connect_args: dict = {}

    if ssl_mode or ssl_ca or ssl_cert or ssl_key:
        mode = str(ssl_mode or "").strip().lower()

        # MySQL/TiDB 常见语义：DISABLED / REQUIRED / VERIFY_CA / VERIFY_IDENTITY
        if mode in ("disabled", "disable", "false", "0", "off"):
            connect_args["ssl"] = False
        elif mode in ("required", "require"):
            ctx = ssl_module.create_default_context(cafile=ssl_ca) if ssl_ca else ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_NONE
            connect_args["ssl"] = ctx
        elif mode in ("verify_ca", "verify-ca"):
            ctx = ssl_module.create_default_context(cafile=ssl_ca) if ssl_ca else ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_REQUIRED
            connect_args["ssl"] = ctx
        else:
            # 默认按 VERIFY_IDENTITY/VERIFY_FULL 处理：校验证书链 + 校验主机名
            ctx = ssl_module.create_default_context(cafile=ssl_ca) if ssl_ca else ssl_module.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl_module.CERT_REQUIRED
            connect_args["ssl"] = ctx

        # 若用户提供了客户端证书/私钥，也加载进去（可选）
        if (ssl_cert or ssl_key) and isinstance(connect_args.get("ssl"), ssl_module.SSLContext):
            try:
                connect_args["ssl"].load_cert_chain(certfile=ssl_cert, keyfile=ssl_key)
            except Exception:
                # 证书不可用时不阻断启动，交给连接阶段报错更直观
                pass

    # TiDB Cloud Serverless 强制 TLS：若连接串未显式指定任何 SSL 参数，但目标是 tidbcloud.com，
    # 则默认启用证书校验 + 主机名校验（等价于 VERIFY_IDENTITY）。
    # 这样可以避免在 Render 等平台上因为“insecure transport prohibited”导致启动失败。
    #
    # 注意：这里不再仅凭端口 4000 自动开启 TLS。很多自托管 TiDB/MySQL 也会使用 4000，
    # 如果一刀切强制 TLS，反而会导致本地/内网部署无法连接。
    if "ssl" not in connect_args:
        hostname = (parts.hostname or "").lower()
        is_private_or_local = False
        if hostname in {"localhost", "127.0.0.1", "::1"}:
            is_private_or_local = True
        else:
            try:
                ip = ipaddress.ip_address(hostname)
                is_private_or_local = ip.is_private or ip.is_loopback or ip.is_link_local
            except Exception:
                # 非 IP（域名）时忽略
                pass

        # 仅在明确是 TiDB Cloud 域名时启用自动 TLS。
        # 若是自定义域名或自托管环境，请在 URL 中显式设置 ?ssl=true / ?ssl_mode=...
        if (
            not is_private_or_local
            and (
                hostname.endswith("tidbcloud.com")
                or hostname.endswith("tidbcloud.com.")
                or "tidbcloud.com" in hostname
            )
        ):
            ctx = ssl_module.create_default_context(cafile=ssl_ca) if ssl_ca else ssl_module.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl_module.CERT_REQUIRED
            connect_args["ssl"] = ctx

    new_query = urlencode(kept, doseq=True)
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    return clean_url, connect_args


def _extract_asyncpg_ssl_connect_args(db_url: str) -> tuple[str, dict]:
    """从 URL query 中提取 PostgreSQL SSL 参数，转换为 asyncpg 可识别的 connect_args。

    背景：
    - 许多平台/服务提供的 DATABASE_URL 会带 `?sslmode=...`
    - 但 asyncpg.connect() 不接受 sslmode 这个关键字参数，会导致：
      `TypeError: connect() got an unexpected keyword argument 'sslmode'`

    处理：
    - 从 URL 中移除 sslmode/sslrootcert 等参数，避免被 SQLAlchemy 透传给 asyncpg
    - 根据 sslmode 构造 asyncpg 所需的 `ssl=` 参数（bool 或 SSLContext）

    返回： (clean_url, connect_args)
    """

    parts = urlsplit(db_url)
    qsl = parse_qsl(parts.query, keep_blank_values=True)

    sslmode = None
    sslrootcert = None
    kept: list[tuple[str, str]] = []

    for k, v in qsl:
        lk = k.lower()
        if lk == "sslmode":
            sslmode = v
        elif lk == "sslrootcert":
            sslrootcert = v
        else:
            kept.append((k, v))

    connect_args: dict = {}

    if sslmode:
        mode = str(sslmode).strip().lower()

        # 参考 libpq sslmode 语义：disable / allow / prefer / require / verify-ca / verify-full
        if mode in ("disable", "false", "0", "off"):
            connect_args["ssl"] = False
        elif mode in ("require",):
            # require：加密但不校验证书
            ctx = ssl_module.create_default_context(cafile=sslrootcert) if sslrootcert else ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_NONE
            connect_args["ssl"] = ctx
        elif mode in ("verify-ca", "verify_ca"):
            # verify-ca：校验证书链，但不校验主机名
            ctx = ssl_module.create_default_context(cafile=sslrootcert) if sslrootcert else ssl_module.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl_module.CERT_REQUIRED
            connect_args["ssl"] = ctx
        else:
            # 默认：prefer / allow / verify-full / 其它
            # verify-full：校验证书链 + 校验主机名（默认 context 就是该语义）
            ctx = ssl_module.create_default_context(cafile=sslrootcert) if sslrootcert else ssl_module.create_default_context()
            # 确保 verify-full 语义
            ctx.check_hostname = True
            ctx.verify_mode = ssl_module.CERT_REQUIRED
            connect_args["ssl"] = ctx

    new_query = urlencode(kept, doseq=True)
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))

    return clean_url, connect_args


from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from contextlib import asynccontextmanager


_legacy_async_session = None
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, Text

# PostgreSQL 下使用 JSONB（更高效/可索引）；其它数据库回退到 JSON
try:
    from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB
except Exception:  # pragma: no cover
    _PG_JSONB = None

from core.log_config import logger

# 定义数据库模型
Base = declarative_base()


# ============== Dialect compatibility helpers ==============

# MySQL/TiDB 在列 DEFAULT 上对表达式支持更严格，且 TiDB Cloud Serverless 强制 TLS。
_IS_MYSQL = (DB_TYPE or "sqlite").lower() == "mysql"

# 用于 DDL 的“当前时间”默认值：
# - Postgres/SQLite：func.now()
# - MySQL/TiDB：CURRENT_TIMESTAMP（避免 DEFAULT now() 导致建表失败）
_SERVER_NOW = text("CURRENT_TIMESTAMP") if _IS_MYSQL else func.now()

# MySQL/TiDB 方言要求 VARCHAR 必须指定长度；同时为避免旧 MySQL/utf8mb4 下索引长度限制，
# 对带索引的列使用更保守的 191。
_VARCHAR = String(255) if _IS_MYSQL else String
_VARCHAR_INDEX = String(191) if _IS_MYSQL else String

class RequestStat(Base):
    __tablename__ = 'request_stats'
    id = Column(Integer, primary_key=True)
    request_id = Column(_VARCHAR)
    endpoint = Column(_VARCHAR)
    client_ip = Column(_VARCHAR)
    process_time = Column(Float)
    first_response_time = Column(Float)
    content_start_time = Column(Float, nullable=True)  # 正文开始时间（首个非空content）
    provider = Column(_VARCHAR_INDEX, index=True)
    model = Column(_VARCHAR_INDEX, index=True)
    api_key = Column(_VARCHAR_INDEX, index=True)
    success = Column(Boolean, default=False, index=True)  # 请求是否成功
    status_code = Column(Integer, nullable=True, index=True)  # HTTP 状态码
    is_flagged = Column(Boolean, default=False)
    text = Column(Text)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    prompt_price = Column(Float, default=0.0)
    completion_price = Column(Float, default=0.0)
    timestamp = Column(DateTime(timezone=True), server_default=_SERVER_NOW, index=True)
    
    # 扩展日志字段
    provider_id = Column(_VARCHAR_INDEX, nullable=True, index=True)  # 渠道ID
    provider_key_index = Column(Integer, nullable=True)  # 渠道使用的上游key索引
    api_key_name = Column(_VARCHAR, nullable=True)  # 使用的key
    api_key_group = Column(_VARCHAR, nullable=True)  # 分组
    retry_count = Column(Integer, default=0)  # 重试次数
    retry_path = Column(Text, nullable=True)  # 重试路径JSON格式
    request_headers = Column(Text, nullable=True)  # 用户请求头JSON格式
    request_body = Column(Text, nullable=True)  # 用户请求体
    upstream_request_headers = Column(Text, nullable=True)  # 发送到上游的请求头JSON格式
    upstream_request_body = Column(Text, nullable=True)  # 发送到上游的请求体
    upstream_response_body = Column(Text, nullable=True)  # 上游返回的原始响应体
    response_body = Column(Text, nullable=True)  # 返回给用户的响应体
    raw_data_expires_at = Column(DateTime(timezone=True), nullable=True)  # 原始数据过期时间

class ChannelStat(Base):
    __tablename__ = 'channel_stats'
    id = Column(Integer, primary_key=True)
    request_id = Column(_VARCHAR)
    provider = Column(_VARCHAR_INDEX, index=True)
    model = Column(_VARCHAR_INDEX, index=True)
    api_key = Column(_VARCHAR)
    provider_api_key = Column(_VARCHAR_INDEX, nullable=True, index=True)
    success = Column(Boolean, default=False)
    timestamp = Column(DateTime(timezone=True), server_default=_SERVER_NOW, index=True)


class AdminUser(Base):
    """管理员账号（用于首次初始化向导 /setup）。

    说明：
    - 仅保存一个管理员（id=1）
    - password_hash 为 PBKDF2-HMAC-SHA256 的字符串格式
    - jwt_secret：用于签发/校验 JWT（若未设置环境变量 JWT_SECRET，会使用该值）
    """

    __tablename__ = "admin_user"

    id = Column(Integer, primary_key=True)
    username = Column(_VARCHAR_INDEX, nullable=False, index=True)
    password_hash = Column(_VARCHAR, nullable=False)
    jwt_secret = Column(_VARCHAR, nullable=True)


class AppConfig(Base):
    """配置存储表（用于将配置入库）。

    说明：
    - DB 作为权威配置源（source of truth）
    - PostgreSQL 使用 JSONB，其它数据库使用 JSON
    - 仅保存“用户配置”（会清理运行时字段，如 _model_dict_cache）
    """

    __tablename__ = "app_config"

    # 固定单行即可（id=1）
    id = Column(Integer, primary_key=True)

    # JSON/JSONB 配置
    # - PostgreSQL/CockroachDB: JSONB（可索引）
    # - SQLite 等其它 DB：回退到 Text
    #
    # 注意：不要在 SQLite 下使用 postgresql.JSONB，否则会在建表阶段报：
    # `SQLiteTypeCompiler can't render element of type JSONB`
    use_jsonb = (DB_TYPE or "sqlite").lower() == "postgres" and _PG_JSONB is not None
    config_json = Column(_PG_JSONB if use_jsonb else Text, nullable=True)

    # 预留：便于人工导出/排查（可选，不参与主流程）
    config_yaml = Column(Text, nullable=True)

    # 最近更新时间（数据库侧 now）
    # MySQL/TiDB: 使用 CURRENT_TIMESTAMP，避免 DEFAULT now() 导致建表失败
    updated_at = Column(
        DateTime(timezone=True),
        server_default=_SERVER_NOW,
        server_onupdate=_SERVER_NOW if _IS_MYSQL else None,
        onupdate=(func.now() if not _IS_MYSQL else None),
        index=True,
    )

# DISABLE_DATABASE=true 可关闭统计数据库（例如无持久化磁盘的免费部署环境）
DISABLE_DATABASE = env_bool("DISABLE_DATABASE", False)
db_engine = None
async_session = None

# D1 运行时对象
d1_client: Optional[D1HTTPClient] = None

if not DISABLE_DATABASE:
    is_debug = env_bool("DEBUG", False)

    # 1) 优先使用 DATABASE_URL（适合云平台）
    # 2) 否则 fallback 到现有 DB_TYPE/DB_* 环境变量
    logger.info(f"Using {DB_TYPE} database.")
    if DATABASE_URL:
        logger.info("DATABASE_URL detected, using it for database connection.")

    if DB_TYPE == "postgres":
        try:
            import asyncpg
        except ImportError:
            raise ImportError("asyncpg is not installed. Please install it with 'pip install asyncpg' to use PostgreSQL.")

        connect_args = {}
        if DATABASE_URL:
            db_url = _normalize_database_url(DATABASE_URL, DB_TYPE)
            # 兼容 ?sslmode=...（asyncpg 不识别 sslmode，需要转换为 ssl 参数）
            db_url, connect_args = _extract_asyncpg_ssl_connect_args(db_url)
        else:
            DB_USER = os.getenv("DB_USER", "postgres")
            DB_PASSWORD = os.getenv("DB_PASSWORD", "mysecretpassword")
            DB_HOST = os.getenv("DB_HOST", "localhost")
            DB_PORT = os.getenv("DB_PORT", "5432")
            DB_NAME = os.getenv("DB_NAME", "postgres")
            db_url = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

        db_engine = create_async_engine(db_url, echo=is_debug, connect_args=connect_args)

    elif DB_TYPE == "mysql":
        # TiDB 兼容（MySQL 协议）
        try:
            import asyncmy  # noqa: F401
        except ImportError:
            raise ImportError(
                "asyncmy is not installed. Please install it with 'pip install asyncmy' to use TiDB/MySQL."
            )

        connect_args = {}
        if DATABASE_URL:
            db_url = _normalize_database_url(DATABASE_URL, DB_TYPE)
            db_url, connect_args = _extract_mysql_ssl_connect_args(db_url)
        else:
            DB_USER = os.getenv("DB_USER", "root")
            DB_PASSWORD = os.getenv("DB_PASSWORD", "")
            DB_HOST = os.getenv("DB_HOST", "localhost")
            DB_PORT = os.getenv("DB_PORT", "4000")  # TiDB 默认 4000
            DB_NAME = os.getenv("DB_NAME", "test")
            db_url = f"mysql+asyncmy://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

        # pool_pre_ping：Render 这类平台上空闲连接容易被断开；开启可减少偶发断连
        db_engine = create_async_engine(
            db_url,
            echo=is_debug,
            connect_args=connect_args,
            pool_pre_ping=True,
        )

    elif DB_TYPE == "sqlite":
        if DATABASE_URL:
            db_url = _normalize_database_url(DATABASE_URL, DB_TYPE)
            # 尝试为文件型 sqlite 创建目录（:memory: 不处理）
            if db_url.startswith("sqlite+aiosqlite:///") and ":memory:" not in db_url:
                raw_path = db_url.split("sqlite+aiosqlite:///", 1)[1].split("?", 1)[0]
                # 允许 ./data/xxx.db
                dir_path = os.path.dirname(raw_path)
                if dir_path:
                    os.makedirs(dir_path, exist_ok=True)
            db_engine = create_async_engine(db_url, echo=is_debug)
        else:
            db_path = os.getenv("DB_PATH", "./data/stats.db")
            data_dir = os.path.dirname(db_path)
            os.makedirs(data_dir, exist_ok=True)
            db_engine = create_async_engine("sqlite+aiosqlite:///" + db_path, echo=is_debug)

        @event.listens_for(db_engine.sync_engine, "connect")
        def set_sqlite_pragma_on_connect(dbapi_connection, connection_record):
            cursor = None
            try:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA busy_timeout = 30000;")  # 30 seconds
                cursor.execute("PRAGMA synchronous = NORMAL;")  # Faster writes
                cursor.execute("PRAGMA cache_size = -64000;")  # 64MB cache
            except Exception as e:
                logger.error(f"Failed to set PRAGMA for SQLite: {e}")
            finally:
                if cursor:
                    cursor.close()
    else:
        if DB_TYPE == "d1":
            if not (D1_ACCOUNT_ID and D1_DATABASE_ID and D1_API_TOKEN):
                raise ValueError(
                    "DB_TYPE=d1 requires D1_ACCOUNT_ID/CF_ACCOUNT_ID, D1_DATABASE_ID and D1_API_TOKEN/CF_API_TOKEN."
                )
            d1_client = D1HTTPClient(
                account_id=D1_ACCOUNT_ID,
                database_id=D1_DATABASE_ID,
                api_token=D1_API_TOKEN,
                api_base_url=D1_API_BASE_URL,
                timeout_seconds=D1_TIMEOUT_SECONDS,
            )
        else:
            raise ValueError(f"Unsupported DB_TYPE: {DB_TYPE}. Please use 'sqlite', 'postgres', 'mysql' or 'd1'.")

    if db_engine is not None:
        _legacy_async_session = sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@asynccontextmanager
async def async_session_scope():
    """统一数据库会话入口。

    - sqlite/postgres：返回 SQLAlchemy AsyncSession
    - d1：返回 D1HTTPClient
    """

    if DISABLE_DATABASE:
        raise RuntimeError("Database is disabled")

    if DB_TYPE == "d1":
        if d1_client is None:
            raise RuntimeError("D1 client is not initialized")
        yield d1_client
        return

    if _legacy_async_session is None:
        raise RuntimeError("Database session factory is not initialized")

    async with _legacy_async_session() as session:
        yield session

# 向后兼容：保留 async_session 变量
if _legacy_async_session is not None:
    async_session = _legacy_async_session
