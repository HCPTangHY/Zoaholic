"""
Admin 管理路由
"""

import os
import string
import secrets
from copy import deepcopy

from fastapi import APIRouter, Depends, Body, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from core.env import env_bool
from core.timeout_settings import get_timeout_defaults, validate_timeout_preferences
from core.utils import parse_rate_limit, ThreadSafeCircularList, ApiKeyRateLimitRegistry
from utils import update_config, API_YAML_PATH, yaml, dump_config_to_json_obj
from core.log_config import apply_backend_log_preferences
from routes.deps import rate_limit_dependency, verify_admin_api_key, get_app

router = APIRouter()


def _rebuild_runtime_rate_limits(app) -> None:
    """
    重建运行中的限流状态。

    说明：
    - /v1/api_config/update 会更新 app.state.config 和 app.state.api_list。
    - 已存在的 key 限流器不会自动刷新。
    - 这里在保存后重建一次，保证新的限流配置生效。
    """
    config = getattr(app.state, "config", {}) or {}
    api_list = getattr(app.state, "api_list", []) or []
    global_preferences = config.get("preferences") or {}
    global_rate_limit = global_preferences.get("rate_limit", "999999/min")
    app.state.global_rate_limit = parse_rate_limit(global_rate_limit)
    app.state.user_api_keys_rate_limit = ApiKeyRateLimitRegistry(
        config_getter=lambda: app.state.config,
        api_list_getter=lambda: app.state.api_list,
    )

    api_keys = config.get("api_keys") or []
    for api_index, api_key in enumerate(api_list):
        key_preferences = {}
        if api_index < len(api_keys) and isinstance(api_keys[api_index], dict):
            key_preferences = api_keys[api_index].get("preferences") or {}

        app.state.user_api_keys_rate_limit[api_key] = ThreadSafeCircularList(
            [api_key],
            key_preferences.get("rate_limit", {"default": "999999/min"}),
            "round_robin",
        )

    # 修改原因：管理端热更新 api.yaml 后，统一配额配置也必须立即生效，不能等服务重启。
    # 修改方式：复用已挂在 app.state 上的 QuotaRegistry，并用最新 config 重新初始化计数器。
    # 目的：保持旧限速重建流程不变，同时让 Phase 2 quota 配置在保存后同步刷新。
    quota_registry = getattr(app.state, 'quota_registry', None)
    if quota_registry is not None:
        quota_registry.init_from_config(config)


def _validate_preferences(preferences, owner: str) -> None:
    """Reject malformed limits before runtime normalization or persistence."""
    from core.quota.parser import parse_key, parse_value
    from core.quota.dimensional import parse_dimensional_rules

    if preferences is None:
        return
    if not isinstance(preferences, dict):
        raise ValueError(f"{owner}.preferences must be an object")
    validate_timeout_preferences(preferences, owner)
    if "rate_limit" in preferences:
        rate = preferences["rate_limit"]
        if not isinstance(rate, (str, dict)):
            raise ValueError(f"{owner}.rate_limit must be a string or object")
        for value in rate.values() if isinstance(rate, dict) else [rate]:
            if not isinstance(value, str):
                raise ValueError(f"{owner}.rate_limit values must be strings")
            parse_rate_limit(value)
    if "quota" in preferences:
        quota = preferences["quota"]
        if not isinstance(quota, dict):
            raise ValueError(f"{owner}.quota must be an object")
        for key, value in quota.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{owner}.quota values must be nonempty strings")
            parse_key(key)
            parse_value(value)
    if "quota_rules" in preferences:
        rules = preferences["quota_rules"]
        if not isinstance(rules, list) or len(parse_dimensional_rules(rules)) != len(rules):
            raise ValueError(f"{owner}.quota_rules contains invalid rules")


def _validate_candidate_shape(config: dict) -> None:
    _validate_preferences(config.get("preferences"), "global")
    for section in ("api_keys", "providers"):
        entries = config.get(section, [])
        if not isinstance(entries, list):
            raise ValueError(f"{section} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Each {section} entry must be an object")
            _validate_preferences(entry.get("preferences"), section)
            if section == "providers":
                for sub in entry.get("sub_channels") or []:
                    if isinstance(sub, dict):
                        preferences = sub.get("preferences")
                        if preferences is not None:
                            if not isinstance(preferences, dict):
                                raise ValueError("sub_channels.preferences must be an object")
                            validate_timeout_preferences(preferences, "sub_channels")


async def _persist_config(app, sections_to_verify=None, changed_providers=None, *, candidate_config=None):
    """Prepare and persist privately, then publish runtime fields without awaiting.

    External file/DB writes are not a distributed transaction: a storage failure
    may require reconciliation, but it never publishes an unverified candidate.
    """
    from copy import copy
    from types import SimpleNamespace
    from core.byok import build_byok_prefixes
    from core.ip_blacklist import apply_runtime_ip_blacklists
    from core.config.file_store import save_api_yaml
    from core.config.db_store import save_config_to_db
    from core.log_config import logger
    from utils import _sanitize_config_for_persistence

    sections_to_verify = list(sections_to_verify) if sections_to_verify is not None else ["providers"]
    config_storage = (os.getenv("CONFIG_STORAGE") or "file").strip().lower()
    save_to_db = config_storage in ("auto", "db")
    save_to_file = config_storage in ("file", "auto") or env_bool("SYNC_CONFIG_TO_FILE", False)
    source = app.state.config if candidate_config is None else candidate_config
    try:
        _validate_candidate_shape(source)
        candidate = _sanitize_config_for_persistence(source)
        config, keys, api_list = await update_config(
            candidate, use_config_url=False, skip_model_fetch=True,
            save_to_file=False, save_to_db=False, changed_providers=changed_providers,
        )
        # Build independently; registry callbacks target the live app after publication.
        staged = SimpleNamespace(state=SimpleNamespace(config=config, api_keys_db=keys, api_list=api_list))
        staged.state.byok_prefixes = build_byok_prefixes(keys)
        apply_runtime_ip_blacklists(staged)
        _rebuild_runtime_rate_limits(staged)
        staged.state.user_api_keys_rate_limit._config_getter = lambda: app.state.config
        staged.state.user_api_keys_rate_limit._api_list_getter = lambda: app.state.api_list
        try:
            from main import init_preference, DEFAULT_TIMEOUT, DEFAULT_KEEPALIVE_INTERVAL, DEFAULT_STREAM_HEADER_TIMEOUT
            staged.state.provider_timeouts = init_preference(config, "model_timeout", DEFAULT_TIMEOUT)
            staged.state.keepalive_interval = init_preference(config, "keepalive_interval", DEFAULT_KEEPALIVE_INTERVAL)
            staged.state.stream_header_timeouts = init_preference(
                config, "stream_header_timeout", DEFAULT_STREAM_HEADER_TIMEOUT, alias_keys=("header_timeout",))
        except ImportError as exc:
            logger.warning("Could not load timeout initialization: %s", exc)

        if save_to_db:
            # Call storage directly: update_config's historical DB fallback swallows errors.
            await save_config_to_db(config)
        if save_to_file:
            save_api_yaml(config)
            with open(API_YAML_PATH, "r", encoding="utf-8") as file:
                saved = yaml.load(file) or {}
            expected = dump_config_to_json_obj(config)
            actual = dump_config_to_json_obj(saved)
            mismatches = [section for section in sections_to_verify
                          if expected.get(section) != actual.get(section)]
            if mismatches:
                raise HTTPException(500, f"Config write verification failed: {mismatches}")

        # No awaits below: retain the latest usage accumulated while storage was pending.
        current_quota = getattr(app.state, "quota_registry", None)
        if current_quota is not None:
            quota = copy(current_quota)
            quota._counters = current_quota._counters.copy()
            quota._dimensional_counters = current_quota._dimensional_counters.copy()
            quota._credential_aliases = current_quota._credential_aliases.copy()
            quota.init_from_config(config)
            staged.state.quota_registry = quota
        apply_backend_log_preferences(config.get("preferences") or {})
        for name, value in vars(staged.state).items():
            setattr(app.state, name, value)
    except HTTPException:
        raise
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"Failed to update/persist config: {exc}") from exc
    return {"save_to_file": save_to_file, "save_to_db": save_to_db,
            "api_yaml_path": API_YAML_PATH if save_to_file else None}


@router.get("/v1/generate-api-key", dependencies=[Depends(rate_limit_dependency)])
async def generate_api_key():
    """
    生成新的 API Key
    """
    # 定义字符集（仅字母数字）
    chars = string.ascii_letters + string.digits
    # 生成 48 个字符的随机字符串
    random_string = ''.join(secrets.choice(chars) for _ in range(48))
    api_key = "zk-" + random_string
    return JSONResponse(content={"api_key": api_key})


@router.get("/v1/api_config", dependencies=[Depends(rate_limit_dependency)])
async def api_config(api_index: int = Depends(verify_admin_api_key)):
    """
    获取当前 API 配置
    """
    app = get_app()
    # 过滤运行时字段和展开的子渠道，返回可持久化的配置
    from utils import _sanitize_config_for_persistence
    clean_config = _sanitize_config_for_persistence(app.state.config)
    encoded_config = jsonable_encoder(clean_config)
    effective_keys = {
        key["id"]: {field: value for field, value in key.items() if not field.startswith("_") and field != "api"}
        for key in app.state.config.get("api_keys", []) if key.get("id")
    }
    return JSONResponse(content={
        "api_config": encoded_config,
        "effective_keys": jsonable_encoder(effective_keys),
        "timeout_defaults": get_timeout_defaults(),
    }, headers={"Cache-Control": "no-store"})


@router.post("/v1/api_config/update", dependencies=[Depends(rate_limit_dependency)])
async def api_config_update(
    api_index: int = Depends(verify_admin_api_key),
    config: dict = Body(...)
):
    """
    更新 API 配置
    """
    app = get_app()
    updated = False
    # 先在独立候选配置上完成关系校验，失败不能污染正在服务的配置。
    from core.key_hierarchy import prepare_key_hierarchy, raw_key_config
    from utils import _sanitize_config_for_persistence
    candidate_config = _sanitize_config_for_persistence(app.state.config)

    replace_all = bool(config.get("replace_all") or config.get("force_shrink"))

    # 支持同时更新 providers、api_keys、preferences 和顶层 ip_blacklist 段，保持与 /v1/api_config 返回结构一致
    if "providers" in config:
        incoming_providers = config["providers"]
        if not isinstance(incoming_providers, list):
            raise HTTPException(status_code=400, detail="providers must be a list.")
        from utils import _sanitize_config_for_persistence
        existing_providers = (_sanitize_config_for_persistence({"providers": app.state.config.get("providers") or []}).get("providers") or [])
        if not replace_all and len(existing_providers) > 5:
            if len(incoming_providers) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Refusing to clear all providers without replace_all=true.",
                )
            if (
                1 <= len(incoming_providers) <= 10
                and len(incoming_providers) < len(existing_providers) - 1
                and all(isinstance(p, dict) and p.get("provider") for p in incoming_providers)
            ):
                # 局部传入少量渠道时自动按 provider 名称原地合并（upsert），防止误清空其余渠道
                merged_providers = list(existing_providers)
                for item in incoming_providers:
                    pname = item.get("provider")
                    idx = _find_provider_index(merged_providers, pname)
                    if idx != -1:
                        old_p = merged_providers[idx]
                        if isinstance(old_p, dict):
                            if ("api" not in item or item.get("api") is None) and old_p.get("api") is not None:
                                item = {**item, "api": old_p["api"]}
                            if ("model" not in item or item.get("model") is None) and old_p.get("model") is not None:
                                item = {**item, "model": old_p["model"]}
                        merged_providers[idx] = item
                    else:
                        merged_providers.append(item)
                incoming_providers = merged_providers
            elif len(incoming_providers) < len(existing_providers) - 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Refusing to shrink providers from {len(existing_providers)} to "
                        f"{len(incoming_providers)} without replace_all=true."
                    ),
                )
        candidate_config["providers"] = deepcopy(incoming_providers)
        updated = True

    if "api_keys" in config:
        incoming_keys = config["api_keys"]
        if not isinstance(incoming_keys, list):
            raise HTTPException(status_code=400, detail="api_keys must be a list.")
        existing_keys = [raw_key_config(key) for key in app.state.config.get("api_keys", [])]
        if not replace_all and len(existing_keys) > 2:
            if len(incoming_keys) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Refusing to clear all api_keys without replace_all=true.",
                )
            if (
                1 <= len(incoming_keys) <= 2
                and len(incoming_keys) < len(existing_keys) - 1
                and all(isinstance(k, dict) and k.get("api") for k in incoming_keys)
            ):
                # 局部传入单个/两个 api_key 时按 api 字段原地合并（upsert），防止误清空其余访问 Key
                merged_keys = list(existing_keys)
                for item in incoming_keys:
                    kval = item.get("api")
                    matched = False
                    for i, ek in enumerate(merged_keys):
                        if isinstance(ek, dict) and ek.get("api") == kval:
                            merged_keys[i] = item
                            matched = True
                            break
                    if not matched:
                        merged_keys.append(item)
                incoming_keys = merged_keys
            elif len(incoming_keys) < len(existing_keys) - 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Refusing to shrink api_keys from {len(existing_keys)} to "
                        f"{len(incoming_keys)} without replace_all=true."
                    ),
                )
        incoming_keys = deepcopy(incoming_keys)
        # 旧客户端按凭据编辑时保留稳定 ID；改名或调整顺序不改变关系。
        ids_by_api = {key.get("api"): key.get("id") for key in existing_keys}
        for key in incoming_keys:
            if not isinstance(key, dict):
                raise HTTPException(400, "Each API Key must be an object")
            existing_id = ids_by_api.get(key.get("api"))
            if existing_id:
                if key.get("id") and key["id"] != existing_id:
                    raise HTTPException(400, "An existing Key ID cannot be replaced")
                key["id"] = existing_id
        remaining_ids = {key.get("id") for key in incoming_keys}
        removed_ids = {key.get("id") for key in existing_keys} - remaining_ids
        for key in existing_keys:
            if key.get("parent_id") in removed_ids:
                raise HTTPException(400, "Move or delete child Keys before deleting their parent")
        candidate_config["api_keys"] = incoming_keys
        updated = True

    if "ip_blacklist" in config:
        # 修改原因：全局 IP 黑名单按需求存放在 api.yaml 顶层，而不是 preferences 内。
        # 修改方式：允许 /v1/api_config/update 直接更新顶层 ip_blacklist，后续由 update_config 校验并规范化。
        # 目的：前端保存全局黑名单后能持久化到 api.yaml 顶层并热更新。
        candidate_config["ip_blacklist"] = config["ip_blacklist"]
        updated = True

    # 更新全局 preferences（包括 SCHEDULING_ALGORITHM 等设置）
    if "preferences" in config:
        if not isinstance(config["preferences"], dict):
            raise HTTPException(400, "preferences must be an object")
        candidate_config.setdefault("preferences", {}).update(config["preferences"])
        # 前端发送 null 表示显式删除该字段，清理掉 None 值
        for k in [k for k, v in candidate_config["preferences"].items() if v is None]:
            del candidate_config["preferences"][k]
        updated = True

    if not updated:
        raise HTTPException(
            status_code=400,
            detail="No updatable sections provided. Allowed keys: providers, api_keys, preferences, ip_blacklist.",
        )

    # 修改原因：持久化流程已经抽到 _persist_config，原接口只需要声明本次改动涉及的 section。
    # 修改方式：按请求中的顶层键生成校验范围，并复用统一 helper 返回的 persisted 信息。
    # 目的：保持原全量配置接口行为不变，同时让单渠道接口共享同一套保存与校验逻辑。
    sections_to_verify = [key for key in ("providers", "api_keys", "preferences", "ip_blacklist") if key in config]
    try:
        _validate_candidate_shape(candidate_config)
        prepared = prepare_key_hierarchy(candidate_config.get("api_keys", []))
        candidate_config["api_keys"] = [raw_key_config(key) for key in prepared]
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    persisted = await _persist_config(app, sections_to_verify=sections_to_verify,
                                      candidate_config=candidate_config)

    return JSONResponse(content={
        "message": "API config updated",
        "persisted": persisted,
    })


def _get_providers_config(app):
    """
    返回可修改的 providers 配置列表。
    """
    # 修改原因：新增 provider 局部接口都要读写 app.state.config["providers"]，分散处理会造成边界行为不一致。
    # 修改方式：集中确保 config 是字典且 providers 是列表，缺失时初始化为空列表。
    # 目的：让新增、更新、删除接口使用同一个配置入口，避免重复防御代码。
    if not isinstance(getattr(app.state, "config", None), dict):
        app.state.config = {}
    providers = app.state.config.setdefault("providers", [])
    if not isinstance(providers, list):
        raise HTTPException(status_code=500, detail="Invalid providers configuration: providers must be a list.")
    return providers


def _find_provider_index(providers, provider_id: str) -> int:
    """
    按 provider 字段查找渠道下标。
    """
    # 修改原因：provider_id 的定位规则必须严格按 provider["provider"] == provider_id 执行。
    # 修改方式：遍历 providers 列表，只匹配字典对象中的 provider 字段。
    # 目的：保证 PUT 和 DELETE 对同一 provider_id 的定位行为完全一致。
    for index, provider in enumerate(providers):
        if isinstance(provider, dict) and provider.get("provider") == provider_id:
            return index
    return -1


@router.post("/v1/providers", dependencies=[Depends(rate_limit_dependency)])
async def create_provider(
    api_index: int = Depends(verify_admin_api_key),
    provider_data: dict = Body(...)
):
    """
    新增单个渠道。
    """
    # 修改原因：前端新增渠道不应再提交完整 providers 数组，否则会覆盖其他浏览器中的并发修改。
    # 修改方式：校验 provider 字段和重名冲突后，只把当前 provider 追加到配置列表并复用统一持久化流程。
    # 目的：为新增渠道提供原子化接口，消除全量覆盖风险。
    provider_name = provider_data.get("provider")
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise HTTPException(status_code=400, detail='Provider data must include a non-empty "provider" field.')

    app = get_app()
    candidate = deepcopy(app.state.config)
    providers = candidate["providers"]
    if _find_provider_index(providers, provider_name) != -1:
        raise HTTPException(status_code=409, detail=f'Provider "{provider_name}" already exists.')

    providers.append(provider_data)
    # 增量重建：只重建新增的渠道
    await _persist_config(app, changed_providers={provider_name}, candidate_config=candidate)
    return JSONResponse(
        status_code=201,
        content={"message": "Provider created", "provider_id": provider_name},
    )


@router.get("/v1/providers/{provider_id}", dependencies=[Depends(rate_limit_dependency)])
async def get_provider(
    provider_id: str,
    api_index: int = Depends(verify_admin_api_key),
):
    """
    获取单个渠道的最新配置。
    """
    app = get_app()
    providers = _get_providers_config(app)
    provider_index = _find_provider_index(providers, provider_id)
    if provider_index == -1:
        raise HTTPException(status_code=404, detail=f'Provider "{provider_id}" not found.')

    from utils import _sanitize_config_for_persistence
    clean = _sanitize_config_for_persistence({"providers": [providers[provider_index]]})
    provider_clean = clean["providers"][0] if clean.get("providers") else providers[provider_index]
    return JSONResponse(content={"provider": jsonable_encoder(provider_clean)})


@router.put("/v1/providers/{provider_id}", dependencies=[Depends(rate_limit_dependency)])
async def update_provider(
    provider_id: str,
    api_index: int = Depends(verify_admin_api_key),
    provider_data: dict = Body(...)
):
    """
    更新单个渠道。
    """
    # 修改原因：编辑已有渠道时只应替换目标 provider，不能让前端用旧列表覆盖整个 providers 数组。
    # 修改方式：按路径 provider_id 定位原对象，并用请求体整体替换该对象，不做 deep merge。
    # 目的：允许字段删除和渠道重命名，同时保留其他渠道的最新状态。
    app = get_app()
    candidate = deepcopy(app.state.config)
    providers = candidate["providers"]
    provider_index = _find_provider_index(providers, provider_id)
    if provider_index == -1:
        raise HTTPException(status_code=404, detail=f'Provider "{provider_id}" not found.')

    old_provider = providers[provider_index]
    if isinstance(old_provider, dict):
        if ("api" not in provider_data or provider_data.get("api") is None) and old_provider.get("api") is not None:
            provider_data = {**provider_data, "api": old_provider["api"]}
        if ("model" not in provider_data or provider_data.get("model") is None) and old_provider.get("model") is not None:
            provider_data = {**provider_data, "model": old_provider["model"]}

    providers[provider_index] = provider_data
    # 增量重建：只重建被修改的渠道的 CircularList，其他保持不动
    new_provider_name = provider_data.get('provider') or provider_id
    changed = {provider_id, new_provider_name}  # 支持重命名：旧名+新名
    await _persist_config(app, changed_providers=changed, candidate_config=candidate)
    return JSONResponse(content={"message": "Provider updated", "provider_id": provider_id})


@router.delete("/v1/providers/{provider_id}", dependencies=[Depends(rate_limit_dependency)])
async def delete_provider(
    provider_id: str,
    api_index: int = Depends(verify_admin_api_key),
):
    """
    删除单个渠道。
    """
    # 修改原因：删除渠道时前端不应再上传删除后的完整 providers 数组。
    # 修改方式：后端按 provider_id 在当前配置中移除目标项，并复用统一持久化流程。
    # 目的：避免删除操作把其他设备刚保存的渠道修改一并回滚。
    app = get_app()
    candidate = deepcopy(app.state.config)
    providers = candidate["providers"]
    provider_index = _find_provider_index(providers, provider_id)
    if provider_index == -1:
        raise HTTPException(status_code=404, detail=f'Provider "{provider_id}" not found.')

    providers.pop(provider_index)
    # 删除不需要重建任何 CircularList，但传空 set 会跳过所有重建
    await _persist_config(app, changed_providers=set(), candidate_config=candidate)
    return JSONResponse(content={"message": "Provider deleted", "provider_id": provider_id})