import gc
import os
import json
import asyncio
import tomllib
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from starlette.responses import Response

from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Request

from core.log_config import logger
from routes import api_router
from routes.oauth import router as oauth_router
from core.env import env_bool
from core.log_config import apply_backend_log_preferences
from core.watchdog import EventLoopBlockWatchdog as LightWatchdog
from core.utils import parse_rate_limit, ThreadSafeCircularList, ApiKeyRateLimitRegistry
from core.utils import is_local_api_key
from core.block_watchdog import EventLoopBlockWatchdog
from core.client_manager import ClientManager
from core.channel_manager import ChannelManager
from core.routing import set_debug_mode as set_routing_debug_mode
from core.handler import (
    ModelRequestHandler,
    set_debug_mode as set_handler_debug_mode,
)
from core.middleware import StatsMiddleware, request_info, get_api_key
from core.error_response import openai_error_response

from utils import safe_get, load_config

from db import DISABLE_DATABASE, RequestStat, AdminUser, DB_TYPE, async_session_scope
from core.stats import (
    create_tables,
    update_paid_api_keys_states,
    update_channel_stats,
)
from core.plugins import get_plugin_manager

DEFAULT_TIMEOUT = int(os.getenv("TIMEOUT", 600))
# 修改原因：SSE 流式响应在上游长时间思考/检索/排队时需要应用层心跳，不能依赖 TCP/Nginx keepalive。
# 修改方式：为 keepalive_interval 提供可环境变量覆盖的合理默认值，配置文件仍可按全局/渠道/模型覆盖。
# 目的：默认每 15 秒向下游发送 SSE 注释帧，避免客户端或中间代理因空闲无字节而断开。
DEFAULT_KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", 15))
# DEBUG 环境变量支持 true/false/1/0/yes/no
is_debug = env_bool("DEBUG", False)
logger.info("DISABLE_DATABASE: %s", DISABLE_DATABASE)

# 从 pyproject.toml 读取版本号
try:
    with open('pyproject.toml', 'rb') as f:
        data = tomllib.load(f)
        VERSION = data['project']['version']
except Exception:
    VERSION = 'unknown'
logger.info("VERSION: %s", VERSION)

def init_preference(all_config, preference_key, default_timeout=DEFAULT_TIMEOUT):
    # 存储超时配置
    # 修改原因：旧逻辑在 preferences 为空或未声明某项偏好时，会让 global 默认值变成空 dict，
    # 后续调用方若传入兜底值就可能覆盖启动期 default_timeout（keepalive 因此默认落到 99999 并被禁用）。
    # 修改方式：先写入 default_timeout，再叠加配置文件中的全局/模型级覆盖。
    # 目的：让 model_timeout、keepalive_interval 等偏好都稳定遵守启动期默认值，同时保留现有覆盖语义。
    preference_dict = {"default": default_timeout}
    preferences = safe_get(all_config, "preferences", default={})
    providers = safe_get(all_config, "providers", default=[])
    if preferences:
        if isinstance(preferences.get(preference_key), int):
            preference_dict["default"] = preferences.get(preference_key)
        else:
            preference_settings = preferences.get(preference_key, {}) or {}
            for model_name, timeout_value in preference_settings.items():
                preference_dict[model_name] = timeout_value

    result = defaultdict(lambda: defaultdict(lambda: default_timeout))
    for provider in providers:
        provider_preference_settings = safe_get(provider, "preferences", preference_key, default={})
        if provider_preference_settings:
            for model_name, timeout_value in provider_preference_settings.items():
                result[provider['provider']][model_name] = timeout_value

    result["global"] = preference_dict
    # print("result", json.dumps(result, indent=4))

    return result

async def cleanup_expired_raw_data():
    """
    定时清理过期的原始数据（请求头、请求体、返回体）
    启动时立即执行一次，之后每小时执行一次
    清理已过期的数据字段（保留日志记录本身）

    """
    from sqlalchemy import update
    
    first_run = True
    while True:
        try:
            # 第一次立即执行，之后每小时执行
            if not first_run:
                await asyncio.sleep(3600)
            first_run = False
            
            if DISABLE_DATABASE:
                # 数据库禁用时避免空转；该任务通常不会在 DISABLE_DATABASE=True 时启动，但这里做防御。
                await asyncio.sleep(3600)
                continue
                
            async with async_session_scope() as db:
                now = datetime.now(timezone.utc)

                if (DB_TYPE or "sqlite").lower() == "d1":
                    result = await db.execute(
                        "UPDATE request_stats "
                        # 修改原因：新增 upstream_response_headers 后，过期原始数据清理需要同步覆盖该列。
                        # 修改方式：在 D1 清理 SQL 的 SET 和非空判断中加入 upstream_response_headers。
                        # 目的：避免响应头超过保留期后仍留在 request_stats。
                        "SET request_headers = NULL, request_body = NULL, upstream_request_headers = NULL, upstream_request_body = NULL, upstream_response_headers = NULL, upstream_response_body = NULL, response_body = NULL, retry_path = NULL "
                        "WHERE raw_data_expires_at IS NOT NULL "
                        "AND raw_data_expires_at < ? "
                        "AND (request_headers IS NOT NULL OR request_body IS NOT NULL OR upstream_request_headers IS NOT NULL OR upstream_request_body IS NOT NULL OR upstream_response_headers IS NOT NULL OR upstream_response_body IS NOT NULL OR response_body IS NOT NULL OR retry_path IS NOT NULL)",
                        [now],
                    )
                    rowcount = int((result.get("meta") or {}).get("changes") or 0)
                    if rowcount > 0:
                        logger.info(f"Cleaned up expired raw data from {rowcount} log entries")
                    continue

                # 清理过期的原始数据字段
                # 只清理有过期时间且已过期的记录
                stmt = (
                    update(RequestStat)
                    .where(RequestStat.raw_data_expires_at.isnot(None))
                    .where(RequestStat.raw_data_expires_at < now)
                    .where(
                        (RequestStat.request_headers.isnot(None)) |
                        (RequestStat.request_body.isnot(None)) |
                        (RequestStat.upstream_request_headers.isnot(None)) |
                        (RequestStat.upstream_request_body.isnot(None)) |
                        # 修改原因：SQLAlchemy 分支的过期清理条件也必须包含新增响应头字段。
                        # 修改方式：在非空条件中追加 RequestStat.upstream_response_headers。
                        # 目的：只有响应头未清理的旧记录也能被匹配并清空。
                        (RequestStat.upstream_response_headers.isnot(None)) |
                        (RequestStat.upstream_response_body.isnot(None)) |
                        (RequestStat.response_body.isnot(None)) |
                        (RequestStat.retry_path.isnot(None))
                    )
                    .values(
                        request_headers=None,
                        request_body=None,
                        upstream_request_headers=None,
                        upstream_request_body=None,
                        # 修改原因：清理动作匹配后需要实际清空新增响应头字段。
                        # 修改方式：在 update().values 中把 upstream_response_headers 设为 None。
                        # 目的：保证 SQLAlchemy 数据库类型与 D1 的清理结果一致。
                        upstream_response_headers=None,
                        upstream_response_body=None,
                        response_body=None,
                        retry_path=None,
                    )
                )
                result = await db.execute(stmt)
                await db.commit()

                if result.rowcount > 0:
                    logger.info(f"Cleaned up expired raw data from {result.rowcount} log entries")
                    # SQLite DELETE/UPDATE 不释放磁盘空间，需要 VACUUM 回收 freelist。
                    # 只在 SQLite 且确实清理了数据时执行，避免 PostgreSQL/MySQL 上不必要的开销。
                    # VACUUM 必须在事务外执行，用独立的 raw connection + autocommit。
                    if (DB_TYPE or "sqlite").lower() == "sqlite":
                        try:
                            import aiosqlite
                            db_path = None
                            try:
                                from db import DATABASE_URL
                                if DATABASE_URL and DATABASE_URL.startswith("sqlite"):
                                    db_path = DATABASE_URL.split("///")[-1]
                            except Exception:
                                pass
                            if not db_path:
                                db_path = "data/stats.db"
                            async with aiosqlite.connect(db_path) as vacuum_conn:
                                await vacuum_conn.execute("VACUUM")
                                logger.info("SQLite VACUUM completed after raw data cleanup")
                        except Exception as ve:
                            logger.warning(f"SQLite VACUUM failed (non-critical): {ve}")
                    
        except asyncio.CancelledError:
            logger.info("Raw data cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in raw data cleanup task: {e}")
            # 出错后等待一段时间再重试
            await asyncio.sleep(60)


async def cleanup_expired_logs(app):
    """按全局配置清理过期日志行（删除整行，不保留）。

    说明：
    - 配置项位于 config.preferences：
      - log_retention_mode: keep | manual | auto_delete
      - log_retention_days: 正整数，保留天数
    - 只有 mode == auto_delete 时才执行自动删除（显式开启，避免误删）。
    - 支持固定在每天某个时间点执行（默认 03:00，按服务器时区/可配置时区）。
    """

    from sqlalchemy import delete
    from db import RequestStat, ChannelStat

    def _parse_run_at(value: Optional[str]) -> tuple[int, int]:
        text = str(value or "").strip()
        if not text:
            return 3, 0
        parts = text.split(":")
        try:
            if len(parts) == 1:
                h = int(parts[0])
                m = 0
            else:
                h = int(parts[0])
                m = int(parts[1])
            h = max(0, min(23, h))
            m = max(0, min(59, m))
            return h, m
        except Exception:
            return 3, 0

    def _get_tz(prefs: dict) -> timezone:
        tz_name = str(prefs.get("log_retention_timezone") or "").strip()
        if tz_name:
            try:
                from zoneinfo import ZoneInfo

                return ZoneInfo(tz_name)  # type: ignore
            except Exception:
                pass

        # 默认使用服务器本地时区（容器里通常是 UTC；若你希望用 Asia/Shanghai，可配置 log_retention_timezone）
        try:
            local_tz = datetime.now().astimezone().tzinfo
            if local_tz is not None:
                return local_tz  # type: ignore
        except Exception:
            pass
        return timezone.utc

    def _seconds_until_next_run(now: datetime, hour: int, minute: int) -> int:
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        seconds = int((target - now).total_seconds())
        return max(1, seconds)

    next_sleep_seconds = 0
    while True:
        try:
            if next_sleep_seconds:
                await asyncio.sleep(next_sleep_seconds)
                next_sleep_seconds = 0

            if DISABLE_DATABASE:
                # 防御：数据库禁用时避免空转
                next_sleep_seconds = 3600
                continue

            # 等待配置加载完成（避免启动初期 app.state.config 尚未就绪）
            if not app or not hasattr(app, "state") or not getattr(app.state, "config", None):
                next_sleep_seconds = 5
                continue

            prefs = safe_get(app.state.config, "preferences", default={}) or {}
            mode = str(prefs.get("log_retention_mode") or "").strip().lower()
            days = prefs.get("log_retention_days")

            tz = _get_tz(prefs)
            run_hour, run_minute = _parse_run_at(prefs.get("log_retention_run_at"))
            now_local = datetime.now(tz)

            # 固定在每天指定时间执行。
            # 启动/重启时如果已过今日执行时间，默认等待到下一次执行（避免在任意时间点“补跑”导致误删）。
            # 但为了避免恰好在执行窗口附近重启导致错过当天任务，允许一个宽限窗口（默认 10 分钟）内立即执行。
            target_today = now_local.replace(hour=run_hour, minute=run_minute, second=0, microsecond=0)
            grace_seconds = 10 * 60

            if mode != "auto_delete":
                next_sleep_seconds = _seconds_until_next_run(now_local, run_hour, run_minute)
                continue

            delta_seconds = (now_local - target_today).total_seconds()
            if delta_seconds < 0:
                # 还没到今天的执行时间
                next_sleep_seconds = _seconds_until_next_run(now_local, run_hour, run_minute)
                continue
            if delta_seconds > grace_seconds:
                # 已过执行时间较久：等待下一次执行（通常是明天）
                next_sleep_seconds = _seconds_until_next_run(now_local, run_hour, run_minute)
                continue

            try:
                retention_days = int(days) if days is not None else 30
            except Exception:
                retention_days = 30

            if retention_days <= 0:
                next_sleep_seconds = _seconds_until_next_run(now_local, run_hour, run_minute)
                continue

            now_utc = datetime.now(timezone.utc)
            cutoff = now_utc - timedelta(days=retention_days)

            # ========== D1 ==========
            if (DB_TYPE or "sqlite").lower() == "d1":
                try:
                    from db import d1_client
                except Exception:
                    # d1_client 不可用：等待一段时间再试
                    next_sleep_seconds = 60
                    continue
                if d1_client is None:
                    next_sleep_seconds = 60
                    continue

                # 先删 request_stats，再删 channel_stats
                res1 = await d1_client.execute(
                    "DELETE FROM request_stats WHERE timestamp < ?",
                    [cutoff],
                )
                changes1 = int((res1.get("meta") or {}).get("changes") or 0)

                res2 = await d1_client.execute(
                    "DELETE FROM channel_stats WHERE timestamp < ?",
                    [cutoff],
                )
                changes2 = int((res2.get("meta") or {}).get("changes") or 0)

                if changes1 or changes2:
                    logger.info(
                        f"Auto-deleted expired logs (retention_days={retention_days}): "
                        f"request_stats={changes1}, channel_stats={changes2}"
                    )
                next_sleep_seconds = _seconds_until_next_run(now_local, run_hour, run_minute)
                continue

            # ========== SQLAlchemy (sqlite/postgres/mysql) ==========
            async with async_session_scope() as session:
                # delete request_stats
                r1 = await session.execute(delete(RequestStat).where(RequestStat.timestamp < cutoff))
                # delete channel_stats
                r2 = await session.execute(delete(ChannelStat).where(ChannelStat.timestamp < cutoff))
                await session.commit()

                affected1 = int(r1.rowcount or 0) if hasattr(r1, "rowcount") else 0
                affected2 = int(r2.rowcount or 0) if hasattr(r2, "rowcount") else 0
                if affected1 or affected2:
                    logger.info(
                        f"Auto-deleted expired logs (retention_days={retention_days}): "
                        f"request_stats={affected1}, channel_stats={affected2}"
                    )

            # 安排下一次执行时间
            next_sleep_seconds = _seconds_until_next_run(datetime.now(tz), run_hour, run_minute)

        except asyncio.CancelledError:
            logger.info("Logs retention cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in logs retention cleanup task: {e}")
            next_sleep_seconds = 60


def _register_oauth_providers_from_registry(oauth_manager) -> None:
    """从渠道注册表统一注册 OAuth provider。"""
    # 修改原因：OAuth provider 注册不能继续写在 main.py 的固定渠道清单里，否则内置渠道和插件渠道会走两套路径。
    # 修改方式：遍历 registry 中所有 ChannelDefinition，发现 oauth_provider 后按 channel_id 注册到 OAuthManager。
    # 目的：让 register_channel 成为 OAuth provider 声明的唯一入口，并支持插件在加载后自动加入 OAuthManager。
    from core.channels.registry import get_all_channels

    for channel_id, channel_def in get_all_channels().items():
        oauth_provider = channel_def.oauth_provider
        if oauth_provider is None:
            continue
        bind_oauth_manager = getattr(oauth_provider, "set_oauth_manager", None)
        if callable(bind_oauth_manager):
            # 修改原因：Codex 的被动额度采集仍需要共享 OAuthManager 执行 update_quota，旧硬编码入口移除后必须保留这个绑定点。
            # 修改方式：provider 若声明 set_oauth_manager 钩子，就在通用扫描注册前注入当前 OAuthManager。
            # 目的：保留渠道内部必要副作用，同时不把具体渠道名称重新写回 main.py。
            bind_oauth_manager(oauth_manager)
        oauth_manager.register_provider(channel_id, oauth_provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # gen2 GC 调优：降低触发频率但不完全禁用
    # 默认 (700,10,10) 导致 gen2 频繁触发 stop-the-world 20~30s
    # 改为 (700,50,50)：gen2 触发频率降 5 倍，减少卡顿但仍能回收循环引用
    gc.set_threshold(700, 50, 50)
    logger.info(f"[GC] Tuned thresholds={gc.get_threshold()}")

    # 启动时的代码
    # 设置各模块的调试模式
    set_routing_debug_mode(is_debug)
    set_handler_debug_mode(is_debug)
    app.state.version = VERSION
    app.state.started_at = datetime.now(timezone.utc)
    app.state.startup_completed = False
    
    # 启动定时清理任务
    cleanup_task = None
    logs_cleanup_task = None
    block_watchdog = None
    if not DISABLE_DATABASE:
        try:
            await create_tables()
        except Exception as e:
            # 让云平台的日志里更直观地看到启动失败原因
            logger.exception("Database init failed during startup: %s", e)
            raise

        # 确保 JWT_SECRET 在进程启动后就已确定（避免后端热更新/重启导致前端旧 JWT 立刻 403）
        # 规则：若未显式设置环境变量 JWT_SECRET，则使用 DB 中持久化的 admin_user.jwt_secret。
        try:
            from core.jwt_utils import set_jwt_secret

            if not (os.getenv("JWT_SECRET") or "").strip():
                async with async_session_scope() as db:
                    if (DB_TYPE or "sqlite").lower() == "d1":
                        row = await db.query_one("SELECT jwt_secret FROM admin_user WHERE id = ?", [1])
                        jwt_secret = row.get("jwt_secret") if row else None
                    else:
                        admin_user = await db.get(AdminUser, 1)
                        jwt_secret = getattr(admin_user, "jwt_secret", None) if admin_user is not None else None

                if jwt_secret:
                    set_jwt_secret(str(jwt_secret))
        except Exception as e:
            logger.debug("JWT secret init skipped/failed: %s", e)

        cleanup_task = asyncio.create_task(cleanup_expired_raw_data())
        logger.info("Started raw data cleanup background task")

    if app and not hasattr(app.state, 'config'):
        # logger.warning("Config not found, attempting to reload")
        app.state.config, app.state.api_keys_db, app.state.api_list = await load_config(app)
        # 用于前端判断是否需要进入初始化向导
        app.state.needs_setup = not bool(app.state.api_list)
        # from ruamel.yaml.timestamp import TimeStamp
        # def json_default(obj):
        #     if isinstance(obj, TimeStamp):
        #         return obj.isoformat()
        #     raise TypeError
        # print("app.state.config", json.dumps(app.state.config, indent=4, ensure_ascii=False, default=json_default))

        if app.state.api_list:
            # 使用智能 Registry，自动按需创建限流器
            app.state.user_api_keys_rate_limit = ApiKeyRateLimitRegistry(
                config_getter=lambda: app.state.config,
                api_list_getter=lambda: app.state.api_list
            )
            # 预初始化现有 key 的限流器
            for api_index, api_key in enumerate(app.state.api_list):
                app.state.user_api_keys_rate_limit[api_key] = ThreadSafeCircularList(
                    [api_key],
                    safe_get(app.state.config, 'api_keys', api_index, "preferences", "rate_limit", default={"default": "999999/min"}),
                    "round_robin"
                )
        app.state.global_rate_limit = parse_rate_limit(safe_get(app.state.config, "preferences", "rate_limit", default="999999/min"))

        apply_backend_log_preferences((app.state.config or {}).get("preferences") or {})

        # 如果没有任何 API key，则标记需要初始化并允许服务启动（用于 /setup 初始化向导）
        if not app.state.api_keys_db or not app.state.api_list:
            app.state.needs_setup = True
            app.state.admin_api_key = []
        else:
            app.state.admin_api_key = []
            for item in app.state.api_keys_db:
                if "admin" in item.get("role", ""):
                    app.state.admin_api_key.append(item.get("api"))
            if app.state.admin_api_key == []:
                # 兼容旧配置：如果没显式标记 admin，就默认第一把 key 为 admin
                if len(app.state.api_keys_db) >= 1:
                    app.state.admin_api_key = [app.state.api_keys_db[0].get("api")]

        app.state.provider_timeouts = init_preference(app.state.config, "model_timeout", DEFAULT_TIMEOUT)
        app.state.keepalive_interval = init_preference(app.state.config, "keepalive_interval", DEFAULT_KEEPALIVE_INTERVAL)
        # 初始化 models_list（用于存储从其他 API Key 引用的模型列表）
        app.state.models_list = {}
        # pprint(dict(app.state.provider_timeouts))
        # pprint(dict(app.state.keepalive_interval))
        # print("app.state.provider_timeouts", app.state.provider_timeouts)
        # print("app.state.keepalive_interval", app.state.keepalive_interval)
        if not DISABLE_DATABASE:
            app.state.paid_api_keys_states = {}
            for paid_key in app.state.api_list:
                await update_paid_api_keys_states(app, paid_key)

        # 启动日志行自动清理任务（依赖 config.preferences）
        try:
            logs_cleanup_task = asyncio.create_task(cleanup_expired_logs(app))
            logger.info("Started logs retention cleanup background task")
        except Exception as e:
            logger.error(f"Failed to start logs retention cleanup task: {e}")

    if app and not hasattr(app.state, 'client_manager'):

        default_config = {
            "headers": {
                "User-Agent": "curl/7.68.0",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
            "http2": False,
            "verify": True,
            "follow_redirects": True
        }

        # 初始化客户端管理器（增加连接池以支持长时间请求）
        app.state.client_manager = ClientManager(pool_size=300, max_keepalive_connections=100)
        await app.state.client_manager.init(default_config)

    if app and not hasattr(app.state, 'oauth_manager'):
        # 修改原因：handler 解析 OAuth key_id 时需要访问共享的凭据管理器。
        # 修改方式：在 lifespan 启动期创建 OAuthManager 并加载 data/oauth_state.json。
        # 目的：让请求路径只做内存查找和必要刷新，不在每次请求重复读取文件。
        from core.oauth.manager import OAuthManager
        app.state.oauth_manager = OAuthManager()
        # 修改原因：OAuthManager.init 会把旧扁平 oauth_state.json 按 api.yaml 中的 provider name 自动迁移。
        # 修改方式：先注入 app.state.config getter，再执行 init，让迁移阶段能读取当前 providers 配置。
        # 目的：启动迁移可以把旧凭据放入正确渠道，而不是全部落入 _unmapped。
        app.state.oauth_manager.set_config_ref(lambda: app.state.config or {})
        await app.state.oauth_manager.init()
        # 修改原因：OAuth provider 注册已迁移到 ChannelDefinition.oauth_provider，main.py 不应再知道具体渠道模块。
        # 修改方式：启动时扫描 registry 中所有声明了 oauth_provider 的渠道，并统一注册到 OAuthManager。
        # 目的：消除 Codex、Claude Code、Gemini CLI 等渠道硬编码，让内置渠道和插件渠道共享注册路径。
        _register_oauth_providers_from_registry(app.state.oauth_manager)


    if app and not hasattr(app.state, "channel_manager"):
        if app.state.config and 'preferences' in app.state.config:
            COOLDOWN_PERIOD = app.state.config['preferences'].get('cooldown_period', 300)
        else:
            COOLDOWN_PERIOD = 300

        app.state.channel_manager = ChannelManager(cooldown_period=COOLDOWN_PERIOD)

    if app and not hasattr(app.state, "error_triggers"):
        if app.state.config and 'preferences' in app.state.config:
            ERROR_TRIGGERS = app.state.config['preferences'].get('error_triggers', [])
        else:
            ERROR_TRIGGERS = []
        app.state.error_triggers = ERROR_TRIGGERS

    # 初始化插件系统（扫描 plugins/ 目录并加载所有插件）
    try:
        plugin_manager = get_plugin_manager()
        load_result = plugin_manager.load_all()
        total = sum(len(v) for v in load_result.values())
        enabled = sum(
            len([p for p in group if p.enabled])
            for group in load_result.values()
        )
        logger.info("Plugin system initialized: %d/%d plugins enabled", enabled, total)
        if hasattr(app.state, "oauth_manager"):
            # 修改原因：外置插件渠道通常在 plugin_manager.load_all() 时才调用 register_channel，早于此处的 OAuth 扫描看不到它们。
            # 修改方式：插件加载完成后再次扫描 registry；重复注册内置 provider 只会覆盖为同一个声明实例。
            # 目的：让插件 OAuth 渠道和内置 OAuth 渠道真正走同一条 registry 自动注册路径。
            _register_oauth_providers_from_registry(app.state.oauth_manager)
    except Exception as e:
        logger.error("Failed to initialize plugin system: %s", e)

    if app and not hasattr(app.state, "block_watchdog"):
        try:
            watchdog_settings = safe_get(app.state.config, "preferences", default={}) or {}
            block_watchdog = EventLoopBlockWatchdog.from_settings(watchdog_settings)
            await block_watchdog.start()
            app.state.block_watchdog = block_watchdog
        except Exception as e:
            logger.error(f"Failed to start thread dump watchdog: {e}")

    # 轻量事件循环监控（用于健康检查快照，与 block_watchdog 互补）
    if app and not hasattr(app.state, "event_loop_watchdog"):
        try:
            light_watchdog = LightWatchdog.from_env()
            await light_watchdog.start()
            app.state.event_loop_watchdog = light_watchdog
        except Exception as e:
            logger.error(f"Failed to start event loop watchdog: {e}")

    # 初始化全局 model_handler
    global model_handler
    if model_handler is None:
        model_handler = ModelRequestHandler(
            app=app,
            request_info_getter=request_info.get,
            update_channel_stats_func=update_channel_stats,
            default_timeout=DEFAULT_TIMEOUT,
        )

    # 恢复运行时自动禁用的 Key（从持久化快照）
    try:
        from core.utils import restore_auto_disabled
        restore_auto_disabled()
        logger.info("Restored auto-disabled keys from snapshot")
    except Exception as e:
        logger.debug(f"Failed to restore auto-disabled keys: {e}")

    # 启动一次性初始化 + 统一每日维护循环
    try:
        from core.default_prices import fetch_prices
        await fetch_prices()
    except Exception as e:
        logger.debug(f"Failed to fetch default prices: {e}")
    try:
        from routes.stats import warm_provider_activity
        asyncio.get_running_loop().create_task(warm_provider_activity())
    except Exception as e:
        logger.debug(f"Failed to schedule provider activity warm: {e}")

    async def daily_maintenance():
        """统一每日维护：价格库刷新 + 活跃度刷新"""
        while True:
            await asyncio.sleep(86400)
            try:
                from core.default_prices import fetch_prices
                await fetch_prices(force=True)
                logger.info("[daily_maintenance] Prices refreshed")
            except Exception as e:
                logger.warning(f"[daily_maintenance] Price refresh failed: {e}")
            try:
                from routes.stats import warm_provider_activity
                await warm_provider_activity()
                logger.info("[daily_maintenance] Provider activity refreshed")
            except Exception as e:
                logger.warning(f"[daily_maintenance] Activity refresh failed: {e}")

    asyncio.get_running_loop().create_task(daily_maintenance())

    # 定期 malloc_trim：强制 glibc 归还 free 了但没还给 OS 的内存
    # Python 大字符串（请求/响应体）释放后 pymalloc 标记为可用但 RSS 不降，
    # malloc_trim(0) 让 glibc 把空闲页还给 OS，降低 RSS
    try:
        import ctypes
        _libc = ctypes.CDLL("libc.so.6")
        _has_malloc_trim = hasattr(_libc, 'malloc_trim')
    except Exception:
        _libc = None
        _has_malloc_trim = False

    async def memory_maintenance():
        """每 5 分钟 malloc_trim + 凌晨 4 点 gen2 GC"""
        tick = 0
        while True:
            await asyncio.sleep(300)  # 5 分钟
            tick += 1

            # malloc_trim 每轮都做
            if _has_malloc_trim:
                try:
                    _libc.malloc_trim(0)
                    if tick % 12 == 1:  # 每小时日志一次
                        logger.info("[memory_maintenance] malloc_trim(0) executed")
                except Exception as e:
                    logger.warning(f"[memory_maintenance] malloc_trim failed: {e}")

            # 凌晨 4 点做一次 gen2 GC
            now = datetime.now(timezone(timedelta(hours=8)))  # CST
            if now.hour == 4 and now.minute < 5:
                try:
                    before = gc.get_count()
                    collected = gc.collect()
                    after = gc.get_count()
                    logger.info(f"[memory_maintenance] gen2 collect done: freed {collected} objects, counts {before} -> {after}")
                    if _has_malloc_trim:
                        _libc.malloc_trim(0)
                except Exception as e:
                    logger.warning(f"[memory_maintenance] gc.collect() failed: {e}")

    asyncio.get_running_loop().create_task(memory_maintenance())

    # 启动完成，删除热重载标记文件（通知 monitor 服务已恢复）
    _reload_marker = os.path.join(os.path.dirname(__file__), 'data', '.reloading')
    try:
        os.remove(_reload_marker)
    except FileNotFoundError:
        pass

    app.state.startup_completed = True
    yield
    # 关闭时的代码
    # 写热重载标记文件（通知 monitor 跳过检查）
    try:
        os.makedirs(os.path.dirname(_reload_marker), exist_ok=True)
        with open(_reload_marker, 'w') as f:
            f.write(str(os.getpid()))
        logger.info("[lifespan] Wrote .reloading marker for health monitor")
    except Exception:
        pass

    # 取消清理任务
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass

    if logs_cleanup_task:
        logs_cleanup_task.cancel()
        try:
            await logs_cleanup_task
        except asyncio.CancelledError:
            pass
    
    app.state.startup_completed = False
    if hasattr(app.state, 'block_watchdog'):
        try:
            await app.state.block_watchdog.stop()
        except Exception as e:
            logger.error(f"Failed to stop thread dump watchdog: {e}")
        finally:
            delattr(app.state, 'block_watchdog')

    if hasattr(app.state, 'event_loop_watchdog'):
        try:
            await app.state.event_loop_watchdog.stop()
        except Exception as e:
            logger.error(f"Failed to stop event loop watchdog: {e}")
        finally:
            delattr(app.state, 'event_loop_watchdog')

    # await app.state.client.aclose()
    if hasattr(app.state, 'client_manager'):
        await app.state.client_manager.close()

    # 关闭 file_utils 的共享 HTTP 客户端
    try:
        from core.file_utils import close_shared_fetch_client
        await close_shared_fetch_client()
    except Exception as e:
        logger.error(f"Failed to close shared fetch client: {e}")

app = FastAPI(lifespan=lifespan, debug=is_debug)
app.include_router(api_router)
app.include_router(oauth_router)


def generate_markdown_docs():
    openapi_schema = app.openapi()

    markdown = f"# {openapi_schema['info']['title']}\n\n"
    markdown += f"Version: {openapi_schema['info']['version']}\n\n"
    markdown += f"{openapi_schema['info'].get('description', '')}\n\n"

    markdown += "## API Endpoints\n\n"

    paths = openapi_schema['paths']
    for path, path_info in paths.items():
        for method, operation in path_info.items():
            markdown += f"### {method.upper()} {path}\n\n"
            markdown += f"{operation.get('summary', '')}\n\n"
            markdown += f"{operation.get('description', '')}\n\n"

            if 'parameters' in operation:
                markdown += "Parameters:\n"
                for param in operation['parameters']:
                    markdown += f"- {param['name']} ({param['in']}): {param.get('description', '')}\n"

            markdown += "\n---\n\n"

    return markdown

@app.get("/docs/markdown")
async def get_markdown_docs():
    markdown = generate_markdown_docs()
    return Response(
        content=markdown,
        media_type="text/markdown"
    )

@app.get("/-/health")
async def health_check():
    """轻量级健康探针，用于进程管理器判断存活状态"""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

# 自定义 RequestValidationError 处理已移除，如需可在单独模块中实现

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 404:
        token = await get_api_key(request)
        logger.error(f"404 Error: {exc.detail} api_key: {token}")
    return openai_error_response(message=str(exc.detail), status_code=exc.status_code)


# 配置 CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有头部字段
)

app.add_middleware(StatsMiddleware, debug=is_debug)

@app.middleware("http")
async def ensure_config(request: Request, call_next):
    # 避免在 /v1 请求内进行自调用，防止递归卡死
    if request.url.path.startswith("/v1"):
        return await call_next(request)

    if app and app.state.api_keys_db and not hasattr(app.state, "models_list"):
        app.state.models_list = {}
        for item in app.state.api_keys_db:
            api_key_model_list = item.get("model", [])
            for provider_rule in api_key_model_list:
                provider_name = provider_rule.split("/")[0]
                if is_local_api_key(provider_name) and provider_name in app.state.api_list:
                    models_list = []
                    try:
                        # 构建请求头
                        headers = {
                            "Authorization": f"Bearer {provider_name}"
                        }
                        # 发送GET请求获取模型列表
                        base_url = "http://127.0.0.1:8000/v1/models"
                        async with app.state.client_manager.get_client(base_url) as client:
                            response = await client.get(base_url, headers=headers)
                            if response.status_code == 200:
                                models_data = response.json()
                                # 将获取到的模型添加到models_list
                                for model in models_data.get("data", []):
                                    models_list.append(model["id"])
                    except Exception as e:
                        if str(e):
                            logger.error(f"获取模型列表失败: {str(e)}")
                    app.state.models_list[provider_name] = models_list
    return await call_next(request)


# ModelRequestHandler 实例，将在应用生命周期中初始化
model_handler: Optional[ModelRequestHandler] = None



# SPA 前端路由 fallback - 所有未匹配的前端路由都返回 index.html
from fastapi.responses import FileResponse

SPA_ROUTES = ["/channels", "/playground", "/admin", "/settings", "/logs", "/login"]

# 缓存控制头：index.html 不缓存，静态资源（带 hash）长期缓存
HTML_NO_CACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}
ASSET_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}  # 1 年

@app.get("/{path:path}")
async def spa_fallback(path: str):
    index_html = "./static/index.html"

    # 检查是否是前端 SPA 路由
    if path == "" or any(path.startswith(route.lstrip("/")) for route in SPA_ROUTES):
        if os.path.isfile(index_html):
            return FileResponse(index_html, headers=HTML_NO_CACHE_HEADERS)
        # 未构建前端时，不要 500；提示用户如何生成
        return JSONResponse(
            status_code=404,
            content={
                "detail": "Frontend is not built. Run `cd frontend && npm install && npm run build` or deploy via Docker image.",
            },
        )

    # 尝试返回静态文件
    static_file = f"./static/{path}"
    if os.path.isfile(static_file):
        # 带 hash 的静态资源可以长期缓存
        if "/assets/" in path or path.endswith((".js", ".css", ".woff2", ".woff", ".ttf")):
            return FileResponse(static_file, headers=ASSET_CACHE_HEADERS)
        return FileResponse(static_file)

    # 默认返回 index.html（若存在）
    if os.path.isfile(index_html):
        return FileResponse(index_html, headers=HTML_NO_CACHE_HEADERS)

    return JSONResponse(
        status_code=404,
        content={
            "detail": "Frontend is not built.",
        },
    )

# 添加静态文件挂载（用于 assets、icons 等静态资源）
# 注意：当仅提交源代码且未构建前端时，static 目录可能只有 .gitkeep。
# 因此这里只在目录存在时才挂载，避免启动时报错。
if os.path.isdir("./static/assets"):
    app.mount("/assets", StaticFiles(directory="./static/assets"), name="assets")
if os.path.isdir("./static/icons"):
    app.mount("/icons", StaticFiles(directory="./static/icons"), name="icons")

if __name__ == '__main__':
    import uvicorn
    PORT = int(os.getenv("PORT", "8000"))
    RELOAD = os.getenv("RELOAD", "false").lower() in ("true", "1", "yes")
    
    uvicorn_config = {
        "host": "0.0.0.0",
        "port": PORT,
        "proxy_headers": True,
        "forwarded_allow_ips": "*",
        "ws": "none",
        # "log_level": "warning"
    }
    
    if RELOAD:
        uvicorn_config.update({
            "reload": True,
            "reload_dirs": ["./"],
            "reload_includes": ["*.py", "api.yaml"],
            "reload_excludes": ["./data"],
        })
        uvicorn.run("main:app", **uvicorn_config)
    else:
        uvicorn.run(app, **uvicorn_config)
