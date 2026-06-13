"""配置加载入口。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
import base64
import os

import httpx
from yaml import YAMLError

from core.env import env_bool
from core.log_config import logger

from .codec import yaml
from . import file_store as config_file
from .file_store import save_api_yaml
from .service import update_config
from .db_store import load_config_from_db, save_config_to_db


yaml_error_message = None


async def load_config(app=None):
    import os
    import base64

    # 配置来源策略：
    # - file（默认）：以本地 api.yaml 为权威配置源（符合“配置即代码”理念）
    # - auto：兼容云平台场景（DB 优先），若 DB 无配置则回退到文件/CONFIG_URL/ENV 作为“种子”
    # - db：强制优先 DB（无则回退文件/CONFIG_URL/ENV 作为“种子”，并写回 DB）
    # - url：只读 CONFIG_URL
    #
    # 背景：PR #2 引入了“配置入库”能力（DB-first），但这会导致 api.yaml 与 DB 形成“双权威”。
    # 为保持 api.yaml 的绝对权威，这里将默认值改为 file。
    config_storage = (os.getenv("CONFIG_STORAGE") or "file").strip().lower()
    sync_to_file = env_bool("SYNC_CONFIG_TO_FILE", False)

    # 0) 仅当显式使用 db 模式时才尝试 DB（避免 DB 与 api.yaml 双权威）
    if config_storage == "db":
        conf_from_db = await load_config_from_db()
        if conf_from_db:
            config, api_keys_db, api_list = await update_config(
                conf_from_db, use_config_url=False, save_to_file=False
            )
            # 可选：把 DB 配置同步回文件（本地环境可能想要）
            if sync_to_file:
                try:
                    save_api_yaml(config)
                except Exception as e:
                    logger.warning(f"Failed to sync config to api.yaml: {e}")
            # 修改原因：load_config 仍保持三元返回以兼容现有启动代码，BYOK 前缀表通过 app.state 单独保存。
            # 修改方式：当调用方传入 app 时写入 app.state.byok_prefixes。
            # 目的：避免大范围修改启动赋值语句，同时让运行时鉴权能读取 BYOK 前缀表。
            if app is not None:
                from core.byok import build_byok_prefixes
                from core.ip_blacklist import apply_runtime_ip_blacklists
                app.state.byok_prefixes = build_byok_prefixes(api_keys_db)
                # 修改原因：IP 黑名单需要启动后立即进入运行时缓存，而不是等首次保存配置。
                # 修改方式：加载配置并建立 BYOK 前缀后，同步预解析全局和 Key 级 IP 黑名单。
                # 目的：服务启动后的首个请求即可按最新 api.yaml 执行 IP 拦截。
                apply_runtime_ip_blacklists(app, config=config, api_keys_db=api_keys_db)
            return config, api_keys_db, api_list

    # 1) 允许从环境变量直接注入配置（适合无文件挂载的 PaaS）
    # - CONFIG_YAML: 直接 YAML 文本
    # - CONFIG_YAML_BASE64: base64 编码的 YAML 文本
    conf_seed = None
    config_yaml_env = os.getenv("CONFIG_YAML")
    config_yaml_b64 = os.getenv("CONFIG_YAML_BASE64")
    if config_storage in ("auto", "db", "file") and (config_yaml_env or config_yaml_b64):
        try:
            yaml_text = config_yaml_env
            if (not yaml_text) and config_yaml_b64:
                yaml_text = base64.b64decode(config_yaml_b64).decode("utf-8")
            if yaml_text:
                conf_seed = yaml.load(yaml_text)
        except Exception as e:
            logger.error(f"Failed to load config from env (CONFIG_YAML/BASE64): {e}")
            conf_seed = None

    # 2) 尝试本地文件 api.yaml（旧方式）
    if conf_seed is None and config_storage in ("auto", "db", "file"):
        try:
            with open(config_file.API_YAML_PATH, 'r', encoding='utf-8') as file:
                conf_seed = yaml.load(file)
            if not conf_seed:
                logger.error("配置文件 'api.yaml' 为空。请检查文件内容。")
                conf_seed = None
        except FileNotFoundError:
            if config_storage == "file":
                logger.error("'api.yaml' not found. Please check the file path.")
        except YAMLError as e:
            logger.error("配置文件 'api.yaml' 格式不正确。请检查 YAML 格式。%s", e)
            global yaml_error_message
            yaml_error_message = "配置文件 'api.yaml' 格式不正确。请检查 YAML 格式。"
            conf_seed = None
        except OSError as e:
            logger.error(f"open 'api.yaml' failed: {e}")
            conf_seed = None

    # 3) 尝试 CONFIG_URL
    if conf_seed is None and config_storage in ("auto", "db", "url"):
        config_url = os.environ.get('CONFIG_URL')
        if config_url:
            try:
                default_config = {
                    "headers": {
                        "User-Agent": "curl/7.68.0",
                        "Accept": "*/*",
                    },
                    "http2": True,
                    "verify": True,
                    "follow_redirects": True
                }
                timeout = httpx.Timeout(
                    connect=15.0,
                    read=100,
                    write=30.0,
                    pool=200
                )
                client = httpx.AsyncClient(
                    timeout=timeout,
                    **default_config
                )
                response = await client.get(config_url)
                response.raise_for_status()
                conf_seed = yaml.load(response.text)
            except Exception as e:
                logger.error(f"Error fetching or parsing config from {config_url}: {str(e)}")
                conf_seed = None

    if not conf_seed or not isinstance(conf_seed, dict):
        # 兜底：允许用环境变量提供一个“启动用”的管理员 key，便于在云平台上首次启动后
        # 通过 /admin 页面或 /v1/api_config/update 完成配置，并把配置持久化到数据库。
        #
        # 支持：
        # - ADMIN_API_KEY=zk-xxxx
        # - ADMIN_API_KEYS=zk-xxx,zk-yyy
        admin_keys_raw = (os.getenv("ADMIN_API_KEYS") or os.getenv("ADMIN_API_KEY") or "").strip()
        if admin_keys_raw:
            admin_keys = [k.strip() for k in admin_keys_raw.split(",") if k.strip()]
            if admin_keys:
                conf_seed = {
                    "providers": [],
                    "api_keys": [
                        {
                            "api": k,
                            "role": "admin",
                            # 保持与原配置结构一致，后续 update_config 会进一步规范化
                            "model": ["all"],
                        }
                        for k in admin_keys
                    ],
                    "preferences": {},
                }
        if not conf_seed or not isinstance(conf_seed, dict):
            if app is not None:
                app.state.byok_prefixes = []
                from core.ip_blacklist import apply_runtime_ip_blacklists
                # 修改原因：无配置启动时也应初始化空黑名单缓存，避免鉴权路径读取缺失属性。
                # 修改方式：对空 config/app state 执行一次运行时缓存重建。
                # 目的：让后续首次初始化配置前的健康检查和管理路由保持稳定。
                apply_runtime_ip_blacklists(app)
            return {}, {}, []

    # 4) 规范化配置（不写回文件，避免启动时污染）
    config, api_keys_db, api_list = await update_config(
        conf_seed, use_config_url=(config_storage == "url"), save_to_file=False
    )
    if app is not None:
        # 修改原因：update_config 保持旧的三元返回，不把运行时 BYOK 前缀表塞进配置文件。
        # 修改方式：load_config 持有 app 时基于规范化后的 api_keys_db 单独写 app.state.byok_prefixes。
        # 目的：启动加载配置后，所有鉴权入口都能读取 BYOK 前缀表，同时不破坏旧调用方。
        from core.byok import build_byok_prefixes
        from core.ip_blacklist import apply_runtime_ip_blacklists
        app.state.byok_prefixes = build_byok_prefixes(api_keys_db)
        # 修改原因：IP 黑名单是运行时派生状态，不能只保存在 config 字典中。
        # 修改方式：启动加载配置后预解析顶层和 Key 级黑名单到 app.state。
        # 目的：请求鉴权时直接使用 set 和 network 列表匹配，无需重复解析配置。
        apply_runtime_ip_blacklists(app, config=config, api_keys_db=api_keys_db)

    # 5) 如果策略允许且数据库可用：把种子配置写入 DB，作为后续“权威配置”
    if config_storage in ("auto", "db"):
        try:
            await save_config_to_db(config)
        except Exception as e:
            logger.warning(f"Failed to persist config to DB: {e}")

    # 可选：把最终配置同步回本地 api.yaml
    if sync_to_file:
        try:
            save_api_yaml(config)
        except Exception as e:
            logger.warning(f"Failed to sync config to api.yaml: {e}")

    return config, api_keys_db, api_list
