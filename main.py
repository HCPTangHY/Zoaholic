import os
import json
import asyncio
import tomllib
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from starlette.responses import Response

from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Request

from core.log_config import logger
from routes import api_router
from core.env import env_bool
from core.utils import parse_rate_limit, ThreadSafeCircularList, ApiKeyRateLimitRegistry
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
    preference_dict = {}
    preferences = safe_get(all_config, "preferences", default={})
    providers = safe_get(all_config, "providers", default=[])
    if preferences:
        if isinstance(preferences.get(preference_key), int):
            preference_dict["default"] = preferences.get(preference_key)
        else:
            for model_name, timeout_value in preferences.get(preference_key, {"default": default_timeout}).items():
                preference_dict[model_name] = timeout_value
            if "default" not in preferences.get(preference_key, {}):
                preference_dict["default"] = default_timeout

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
                continue
                
            async with async_session_scope() as db:
                now = datetime.now(timezone.utc)

                if (DB_TYPE or "sqlite").lower() == "d1":
                    result = await db.execute(
                        "UPDATE request_stats "
                        "SET request_headers = NULL, request_body = NULL, response_body = NULL "
                        "WHERE raw_data_expires_at IS NOT NULL "
                        "AND raw_data_expires_at < ? "
                        "AND (request_headers IS NOT NULL OR request_body IS NOT NULL OR response_body IS NOT NULL)",
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
                        (RequestStat.response_body.isnot(None))
                    )
                    .values(
                        request_headers=None,
                        request_body=None,
                        response_body=None
                    )
                )
                result = await db.execute(stmt)
                await db.commit()
                
                if result.rowcount > 0:
                    logger.info(f"Cleaned up expired raw data from {result.rowcount} log entries")
                    
        except asyncio.CancelledError:
            logger.info("Raw data cleanup task cancelled")
            break
        except Exception as e:
            logger.error(f"Error in raw data cleanup task: {e}")
            # 出错后等待一段时间再重试
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时的代码
    # 设置各模块的调试模式
    set_routing_debug_mode(is_debug)
    set_handler_debug_mode(is_debug)
    
    # 启动定时清理任务
    cleanup_task = None
    if not DISABLE_DATABASE:
        try:
            await create_tables()
        except Exception as e:
            # 让 Render 等平台的日志里更直观地看到启动失败原因
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
        app.state.keepalive_interval = init_preference(app.state.config, "keepalive_interval", 99999)
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

    if app and not hasattr(app.state, 'client_manager'):

        default_config = {
            "headers": {
                "User-Agent": "curl/7.68.0",
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            },
            "http2": True,
            "verify": True,
            "follow_redirects": True
        }

        # 初始化客户端管理器（增加连接池以支持长时间请求）
        app.state.client_manager = ClientManager(pool_size=300, max_keepalive_connections=100)
        await app.state.client_manager.init(default_config)


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
    except Exception as e:
        logger.error("Failed to initialize plugin system: %s", e)

    # 初始化全局 model_handler
    global model_handler
    if model_handler is None:
        model_handler = ModelRequestHandler(
            app=app,
            request_info_getter=request_info.get,
            update_channel_stats_func=update_channel_stats,
            default_timeout=DEFAULT_TIMEOUT,
        )

    yield
    # 关闭时的代码
    # 取消清理任务
    if cleanup_task:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
    
    # await app.state.client.aclose()
    if hasattr(app.state, 'client_manager'):
        await app.state.client_manager.close()

app = FastAPI(lifespan=lifespan, debug=is_debug)
app.include_router(api_router)


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
                if provider_name.startswith("sk-") and provider_name in app.state.api_list:
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
