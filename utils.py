import json
import httpx
import asyncio
import h2.exceptions
from time import time
import time as time_module
from fastapi import HTTPException
from collections import defaultdict
from typing import List, Dict, Optional
from ruamel.yaml import YAML, YAMLError
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func, case
from db import async_session, ChannelStat, DISABLE_DATABASE

from core.log_config import logger
from core.utils import (
    safe_get,
    get_model_dict,
    ThreadSafeCircularList,
    provider_api_circular_list,
)

class InMemoryRateLimiter:
    def __init__(self):
        self.requests = defaultdict(list)

    async def is_rate_limited(self, key: str, limits) -> bool:
        now = time()

        # 检查所有速率限制条件
        for limit, period in limits:
            # 计算在当前时间窗口内的请求数量
            recent_requests = sum(1 for req in self.requests[key] if req > now - period)
            if recent_requests >= limit:
                return True

        # 清理太旧的请求记录（比最长时间窗口还要老的记录）
        max_period = max(period for _, period in limits)
        self.requests[key] = [req for req in self.requests[key] if req > now - max_period]

        # 记录新的请求
        self.requests[key].append(now)
        return False

from ruamel.yaml.scalarstring import DoubleQuotedScalarString

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=4, offset=2)

API_YAML_PATH = "./api.yaml"
yaml_error_message = None

def _quote_colon_strings(obj):
    """
    递归处理配置数据，对包含冒号的纯字符串进行引号包裹，
    避免 YAML 将其解析为键值对。
    """
    if isinstance(obj, str):
        # 如果字符串包含冒号，使用双引号包裹
        if ':' in obj:
            return DoubleQuotedScalarString(obj)
        return obj
    elif isinstance(obj, dict):
        return {k: _quote_colon_strings(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_quote_colon_strings(item) for item in obj]
    else:
        return obj

def save_api_yaml(config_data):
    # 深拷贝配置数据并处理包含冒号的字符串
    import copy
    processed_data = copy.deepcopy(config_data)
    
    # 清理运行时字段（以 _ 开头的字段不应该被保存到配置文件）
    for provider in processed_data.get('providers', []):
        keys_to_remove = [k for k in list(provider.keys()) if k.startswith('_')]
        for k in keys_to_remove:
            del provider[k]
    
    for api_key in processed_data.get('api_keys', []):
        keys_to_remove = [k for k in list(api_key.keys()) if k.startswith('_')]
        for k in keys_to_remove:
            del api_key[k]
    
    processed_data = _quote_colon_strings(processed_data)
    with open(API_YAML_PATH, "w", encoding="utf-8") as f:
        yaml.dump(processed_data, f)

async def update_config(config_data, use_config_url=False, skip_model_fetch=False, save_to_file=True):
    for index, provider in enumerate(config_data['providers']):
        if provider.get('project_id'):
            if "google-vertex-ai" not in provider.get("base_url", ""):
                provider['base_url'] = 'https://aiplatform.googleapis.com/'
        if provider.get('cf_account_id'):
            provider['base_url'] = 'https://api.cloudflare.com/'

        if isinstance(provider['provider'], int):
            provider['provider'] = str(provider['provider'])

        provider_api = provider.get('api', None)
        if provider_api:
            if isinstance(provider_api, int):
                provider_api = str(provider_api)
            if isinstance(provider_api, str):
                provider_api_circular_list[provider['provider']] = ThreadSafeCircularList(
                    items=[provider_api],
                    rate_limit=safe_get(provider, "preferences", "api_key_rate_limit", default={"default": "999999/min"}),
                    schedule_algorithm=safe_get(provider, "preferences", "api_key_schedule_algorithm", default="round_robin"),
                    provider_name=provider['provider']
                )
            if isinstance(provider_api, list):
                provider_api_circular_list[provider['provider']] = ThreadSafeCircularList(
                    items=provider_api,
                    rate_limit=safe_get(provider, "preferences", "api_key_rate_limit", default={"default": "999999/min"}),
                    schedule_algorithm=safe_get(provider, "preferences", "api_key_schedule_algorithm", default="round_robin"),
                    provider_name=provider['provider']
                )

        if "models.inference.ai.azure.com" in provider['base_url'] and not provider.get("model"):
            provider['model'] = [
                "gpt-4o",
                "gpt-4.1",
                "gpt-4o-mini",
                "o4-mini",
                "o3",
                "text-embedding-3-small",
                "text-embedding-3-large",
            ]

        if provider.get("tools") is None:
            provider["tools"] = True

        provider["_model_dict_cache"] = get_model_dict(provider)
        
        # 规范化渠道分组字段，支持单值与多值
        groups = provider.get("groups")
        if groups is None:
            if isinstance(provider.get("group"), (str, list)):
                groups = provider.get("group")
            elif safe_get(provider, "preferences", "group", default=None):
                groups = safe_get(provider, "preferences", "group", default=None)
        if isinstance(groups, str):
            groups = [groups]
        elif not isinstance(groups, list):
            groups = ["default"]
        if not groups:
            groups = ["default"]
        provider["groups"] = groups
        
        config_data['providers'][index] = provider

    for index, api_key in enumerate(config_data['api_keys']):
        if "api" in api_key:
            config_data['api_keys'][index]["api"] = str(api_key["api"])

    api_keys_db = config_data['api_keys']

    for index, api_key in enumerate(config_data['api_keys']):
        weights_dict = {}
        models = []
        
        # 规范化 API Key 分组字段，支持单值与多值
        key_groups = api_key.get("groups")
        if key_groups is None:
            if isinstance(api_key.get("group"), (str, list)):
                key_groups = api_key.get("group")
            elif safe_get(api_key, "preferences", "group", default=None):
                key_groups = safe_get(api_key, "preferences", "group", default=None)
        if isinstance(key_groups, str):
            key_groups = [key_groups]
        elif not isinstance(key_groups, list):
            key_groups = ["default"]
        if not key_groups:
            key_groups = ["default"]
        config_data['api_keys'][index]['groups'] = key_groups

        # 确保api字段为字符串类型
        if "api" in api_key:
            config_data['api_keys'][index]["api"] = str(api_key["api"])

        if api_key.get('model'):
            for model in api_key.get('model'):
                if isinstance(model, dict):
                    key, value = list(model.items())[0]
                    provider_name = key.split("/")[0]
                    model_name = key.split("/")[1]

                    for provider_item in config_data["providers"]:
                        if provider_item['provider'] != provider_name:
                            continue
                        model_dict = get_model_dict(provider_item)
                        if model_name in model_dict.keys():
                            weights_dict.update({provider_name + "/" + model_name: int(value)})
                        elif model_name == "*":
                            weights_dict.update({provider_name + "/" + model_name: int(value) for model_item in model_dict.keys()})

                    models.append(key)
                if isinstance(model, str):
                    models.append(model)
            if weights_dict:
                config_data['api_keys'][index]['weights'] = weights_dict
            config_data['api_keys'][index]['model'] = models
            api_keys_db[index]['model'] = models
        else:
            # Default to all models if 'model' field is not set
            config_data['api_keys'][index]['model'] = ["all"]
            api_keys_db[index]['model'] = ["all"]

    api_list = [item["api"] for item in api_keys_db]
    # logger.info(json.dumps(config_data, indent=4, ensure_ascii=False))

    # 管理阶段：只在显式请求保存时（save_to_file=True）才同步写回本地 api.yaml，
    # 这样 /v1/api_config/update 等管理接口修改后的配置可以持久化，供其他组件/环境复用。
    # 启动时加载配置不应该触发保存，避免自动添加的字段污染原始配置文件。
    if not use_config_url and save_to_file:
        save_api_yaml(config_data)

    return config_data, api_keys_db, api_list

# 读取YAML配置文件
async def load_config(app=None):
    import os
    try:
        with open(API_YAML_PATH, 'r', encoding='utf-8') as file:
            conf = yaml.load(file)

        if conf:
            # 启动时加载配置，不要自动保存文件，避免污染原始配置
            config, api_keys_db, api_list = await update_config(conf, use_config_url=False, save_to_file=False)
        else:
            logger.error("配置文件 'api.yaml' 为空。请检查文件内容。")
            config, api_keys_db, api_list = {}, {}, []
    except FileNotFoundError:
        if not os.environ.get('CONFIG_URL'):
            logger.error("'api.yaml' not found. Please check the file path.")
        config, api_keys_db, api_list = {}, {}, []
    except YAMLError as e:
        logger.error("配置文件 'api.yaml' 格式不正确。请检查 YAML 格式。%s", e)
        global yaml_error_message
        yaml_error_message = "配置文件 'api.yaml' 格式不正确。请检查 YAML 格式。"
        config, api_keys_db, api_list = {}, {}, []
    except OSError as e:
        logger.error(f"open 'api.yaml' failed: {e}")
        config, api_keys_db, api_list = {}, {}, []

    if config != {}:
        return config, api_keys_db, api_list

    # 新增： 从环境变量获取配置URL并拉取配置
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
            # 初始化客户端管理器
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
            # logger.info(f"Fetching config from {response.text}")
            response.raise_for_status()
            config_data = yaml.load(response.text)
            # 更新配置
            # logger.info(config_data)
            if config_data:
                # 从 CONFIG_URL 加载的配置，不保存到本地文件
                config, api_keys_db, api_list = await update_config(config_data, use_config_url=True, save_to_file=False)
            else:
                logger.error(f"Error fetching or parsing config from {config_url}")
                config, api_keys_db, api_list = {}, {}, []
        except Exception as e:
            logger.error(f"Error fetching or parsing config from {config_url}: {str(e)}")
            config, api_keys_db, api_list = {}, {}, []
    return config, api_keys_db, api_list

async def ensure_string(item):
    if isinstance(item, (bytes, bytearray)):
        return item.decode("utf-8")
    elif isinstance(item, str):
        return item
    elif isinstance(item, dict):
        json_str = await asyncio.to_thread(json.dumps, item)
        return f"data: {json_str}\n\n"
    else:
        return str(item)

def identify_audio_format(file_bytes):
    # 读取开头的字节
    if file_bytes.startswith(b'\xFF\xFB') or file_bytes.startswith(b'\xFF\xF3'):
        return "MP3"
    elif file_bytes.startswith(b'ID3'):
        return "MP3 with ID3"
    elif file_bytes.startswith(b'OpusHead'):
        return "OPUS"
    elif file_bytes.startswith(b'ADIF'):
        return "AAC (ADIF)"
    elif file_bytes.startswith(b'\xFF\xF1') or file_bytes.startswith(b'\xFF\xF9'):
        return "AAC (ADTS)"
    elif file_bytes.startswith(b'fLaC'):
        return "FLAC"
    elif file_bytes.startswith(b'RIFF') and file_bytes[8:12] == b'WAVE':
        return "WAV"
    return "Unknown/PCM"

async def wait_for_timeout(wait_for_thing, timeout = 3, wait_task=None):
    # 创建一个任务来获取第一个响应，但不直接中断生成器
    if wait_task is None:
        first_response_task = asyncio.create_task(wait_for_thing.__anext__())
    else:
        first_response_task = wait_task

    # 创建一个超时任务
    timeout_task = asyncio.create_task(asyncio.sleep(timeout))

    # 等待任意一个任务完成
    done, pending = await asyncio.wait(
        [first_response_task, timeout_task],
        return_when=asyncio.FIRST_COMPLETED
    )

    # 成功返回
    if first_response_task in done:
        # 取消超时任务
        timeout_task.cancel()
        return first_response_task.result(), "success"

    # 超时返回
    else:
        return first_response_task, "timeout"

async def error_handling_wrapper(generator, channel_id, engine, stream, error_triggers, keepalive_interval=None, last_message_role=None):

    async def new_generator(first_item=None, with_keepalive=False, wait_task=None, timeout=3):
        if first_item:
            yield await ensure_string(first_item)

        # 如果需要心跳机制但不使用嵌套生成器方式
        if with_keepalive:
            yield ": keepalive\n\n"
            while True:
                try:
                    item, status = await wait_for_timeout(generator, timeout=timeout, wait_task=wait_task)
                    if status == "timeout":
                        yield ": keepalive\n\n"
                    else:
                        yield await ensure_string(item)
                        wait_task = None
                except asyncio.CancelledError:
                    # 处理客户端断开连接
                    logger.debug(f"provider: {channel_id:<11} Stream cancelled by client in main loop")
                    break
                except Exception:
                    # 捕获任何其他异常
                    # import traceback
                    # error_stack = traceback.format_exc()
                    # error_message = error_stack.split("\n")[-2]
                    # logger.info(f"provider: {channel_id:<11} keepalive loop: {error_message}")
                    break
        else:
            # 原始的逻辑，当不需要心跳时
            try:
                async for item in generator:
                    yield await ensure_string(item)
            except asyncio.CancelledError:
                # 客户端断开连接是正常行为，不需要记录错误日志
                logger.debug(f"provider: {channel_id:<11} Stream cancelled by client")
                return
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.WriteError, httpx.ProtocolError, h2.exceptions.ProtocolError) as e:
                # 网络错误
                logger.error(f"provider: {channel_id:<11} Network error in new_generator: {e}")
                yield "data: [DONE]\n\n"
                return

    start_time = time_module.time()
    try:
        # 创建一个任务来获取第一个响应，但不直接中断生成器
        if keepalive_interval and stream:
            first_item, status = await wait_for_timeout(generator, timeout=keepalive_interval)
            if status == "timeout":
                return new_generator(None, with_keepalive=True, wait_task=first_item, timeout=keepalive_interval), 3.1415
        else:
            first_item = await generator.__anext__()

        first_response_time = time_module.time() - start_time
        # 对第一个响应项进行原有的处理逻辑
        first_item_str = first_item
        # logger.info("first_item_str: %s :%s", type(first_item_str), first_item_str)
        if isinstance(first_item_str, (bytes, bytearray)):
            if identify_audio_format(first_item_str) in ["MP3", "MP3 with ID3", "OPUS", "AAC (ADIF)", "AAC (ADTS)", "FLAC", "WAV"]:
                return first_item, first_response_time
            else:
                first_item_str = first_item_str.decode("utf-8")
        if isinstance(first_item_str, str) and not first_item_str.startswith(": keepalive"):
            if first_item_str.startswith("data:"):
                first_item_str = first_item_str.lstrip("data: ")
            if first_item_str.startswith("[DONE]"):
                logger.error(f"provider: {channel_id:<11} error_handling_wrapper [DONE]!")
                raise StopAsyncIteration
            try:
                encode_first_item_str = first_item_str.encode().decode('unicode-escape')
            except UnicodeDecodeError:
                encode_first_item_str = first_item_str
                logger.error(f"provider: {channel_id:<11} error UnicodeDecodeError: %s", first_item_str)
            if any(x in encode_first_item_str for x in error_triggers):
                logger.error(f"provider: {channel_id:<11} error const string: %s", encode_first_item_str)
                raise StopAsyncIteration
            try:
                first_item_str = await asyncio.to_thread(json.loads, first_item_str)
            except json.JSONDecodeError:
                logger.error(f"provider: {channel_id:<11} error_handling_wrapper JSONDecodeError! {repr(first_item_str)}")
                raise StopAsyncIteration

            # minimax
            status_code = safe_get(first_item_str, 'base_resp', 'status_code', default=200)
            if status_code != 200:
                if status_code == 2013:
                    status_code = 400
                if status_code == 1008:
                    status_code = 429
                detail = safe_get(first_item_str, 'base_resp', 'status_msg', default="no error returned")
                raise HTTPException(status_code=status_code, detail=f"{detail}"[:1000])

        # minimax
        if isinstance(first_item_str, dict) and safe_get(first_item_str, "base_resp", "status_msg", default=None) == "success":
            full_audio_hex = safe_get(first_item_str, "data", "audio", default=None)
            audio_bytes = bytes.fromhex(full_audio_hex)
            return audio_bytes, first_response_time

        if isinstance(first_item_str, dict) and 'error' in first_item_str and first_item_str.get('error') != {"message": "","type": "","param": "","code": None}:
            # 如果第一个 yield 的项是错误信息，抛出 HTTPException
            status_code = first_item_str.get('status_code', 500)
            detail = first_item_str.get('details', f"{first_item_str}")
            raise HTTPException(status_code=status_code, detail=f"{detail}"[:1000])

        if isinstance(first_item_str, dict) and safe_get(first_item_str, "choices", 0, "error", default=None):
            # 如果第一个 yield 的项是错误信息，抛出 HTTPException
            status_code = safe_get(first_item_str, "choices", 0, "error", "code", default=500)
            detail = safe_get(first_item_str, "choices", 0, "error", "message", default=f"{first_item_str}")
            raise HTTPException(status_code=status_code, detail=f"{detail}"[:1000])

        finish_reason = safe_get(first_item_str, "choices", 0, "finish_reason", default=None)
        if isinstance(first_item_str, dict) and finish_reason == "PROHIBITED_CONTENT":
            raise HTTPException(status_code=400, detail="PROHIBITED_CONTENT")

        if isinstance(first_item_str, dict) and finish_reason == "stop" and \
        not safe_get(first_item_str, "choices", 0, "message", "content", default=None) and \
        not safe_get(first_item_str, "choices", 0, "delta", "content", default=None) and \
        last_message_role != "assistant":
            raise StopAsyncIteration

        if isinstance(first_item_str, dict) and engine not in ["tts", "embedding", "dalle", "moderation", "whisper"] and not stream:
            if any(x in str(first_item_str) for x in error_triggers):
                logger.error(f"provider: {channel_id:<11} error const string: %s", first_item_str)
                raise StopAsyncIteration
            content = safe_get(first_item_str, "choices", 0, "message", "content", default=None)
            reasoning_content = safe_get(first_item_str, "choices", 0, "message", "reasoning_content", default=None)
            b64_json = safe_get(first_item_str, "data", 0, "b64_json", default=None)
            tool_calls = safe_get(first_item_str, "choices", 0, "message", "tool_calls", default=None)
            if (content == "" or content is None) and (tool_calls == "" or tool_calls is None) and (reasoning_content == "" or reasoning_content is None) and b64_json is None:
                raise StopAsyncIteration

        return new_generator(first_item), first_response_time

    except StopAsyncIteration:
        # 502 Bad Gateway 是一个更合适的状态码，因为它表明作为代理或网关的服务器从上游服务器收到了无效的响应。
        logger.warning(f"provider: {channel_id:<11} empty response [{type(first_item_str)}]: {first_item_str}")
        raise HTTPException(status_code=502, detail="Upstream server returned an empty response.")

def post_all_models(api_index, config, api_list, models_list):
    all_models = []
    unique_models = set()
    # 构建别名归一化映射（alias -> upstream 以及 upstream -> alias）
    alias_keys = set()
    upstream_to_alias = {}
    alias_to_upstream = {}
    for provider_item in config.get("providers", []):
        model_dict = get_model_dict(provider_item)  # alias -> upstream
        for alias, upstream in model_dict.items():
            alias_keys.add(alias)
            alias_to_upstream[alias] = upstream
            if upstream != alias:
                upstream_to_alias[upstream] = alias

    def normalize_model_name(name: str) -> str:
        """将上游原名统一转换为展示别名；若无映射，则保持原名"""
        return upstream_to_alias.get(name, name)

    # 允许分组集合：仅返回与当前 API Key 分组有交集的渠道模型
    api_key_groups = safe_get(config, 'api_keys', api_index, 'groups', default=['default'])
    if isinstance(api_key_groups, str):
        api_key_groups = [api_key_groups]
    if not isinstance(api_key_groups, list) or not api_key_groups:
        api_key_groups = ['default']
    allowed_groups = set(api_key_groups)
    
    if config['api_keys'][api_index]['model']:
        for model in config['api_keys'][api_index]['model']:
            if model == "all":
                # 如果模型名为 all，则返回所有模型（统一为别名并去重），并按分组过滤
                all_models = get_all_models(config, allowed_groups)
                final_models = []
                seen = set()
                for item in all_models:
                    disp = normalize_model_name(item["id"])
                    if disp not in seen:
                        seen.add(disp)
                        item["id"] = disp
                        final_models.append(item)
                return final_models
            if "/" in model:
                provider = model.split("/")[0]
                model = model.split("/")[1]
                if model == "*":
                    if provider.startswith("sk-") and provider in api_list:
                        # 将聚合器返回的上游名转换为展示别名，避免出现“本名+重定向名”重复
                        # 分组过滤：仅当本地聚合器 Key 与当前请求 Key 分组有交集时才包含
                        try:
                            local_index = api_list.index(provider)
                            p_groups = safe_get(config, 'api_keys', local_index, 'groups', default=['default'])
                        except ValueError:
                            p_groups = ['default']
                        if isinstance(p_groups, str):
                            p_groups = [p_groups] if p_groups else ['default']
                        if not isinstance(p_groups, list) or not p_groups:
                            p_groups = ['default']
                        if allowed_groups.intersection(set(p_groups)):
                            for model_item in models_list[provider]:
                                disp = normalize_model_name(model_item)
                                if disp not in unique_models:
                                    unique_models.add(disp)
                                    model_info = {
                                        "id": disp,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                    else:
                        for provider_item in config["providers"]:
                            if provider_item['provider'] != provider:
                                continue
                            # 跳过禁用的渠道
                            if provider_item.get("enabled") is False:
                                continue
                            # 分组过滤：provider 必须与当前 Key 分组有交集
                            p_groups = provider_item.get("groups") or ["default"]
                            if isinstance(p_groups, str):
                                p_groups = [p_groups] if p_groups else ["default"]
                            if not isinstance(p_groups, list) or not p_groups:
                                p_groups = ["default"]
                            if not allowed_groups.intersection(set(p_groups)):
                                continue

                            model_dict = get_model_dict(provider_item)
                            # 剔除被重定向的上游原名，仅保留展示别名
                            upstream_candidates = {v for k, v in model_dict.items() if v != k}
                            # 如果渠道配置了 model_prefix，只展示带前缀的模型名
                            prefix = provider_item.get('model_prefix', '').strip()
                            for model_item in model_dict.keys():
                                if model_item in upstream_candidates:
                                    continue
                                # 如果有前缀，只返回带前缀的模型名
                                if prefix and not model_item.startswith(prefix):
                                    continue
                                if model_item not in unique_models:
                                    unique_models.add(model_item)
                                    model_info = {
                                        "id": model_item,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                        # "owned_by": provider_item['provider']
                                    }
                                    all_models.append(model_info)
                else:
                    if provider.startswith("sk-") and provider in api_list:
                        # 支持别名/上游名两种写法，统一输出为展示别名
                        # 分组过滤：仅当本地聚合器 Key 与当前请求 Key 分组有交集时才包含
                        try:
                            local_index = api_list.index(provider)
                            p_groups = safe_get(config, 'api_keys', local_index, 'groups', default=['default'])
                        except ValueError:
                            p_groups = ['default']
                        if isinstance(p_groups, str):
                            p_groups = [p_groups] if p_groups else ['default']
                        if not isinstance(p_groups, list) or not p_groups:
                            p_groups = ['default']

                        if allowed_groups.intersection(set(p_groups)):
                            upstream_name = alias_to_upstream.get(model, model)
                            if upstream_name in models_list[provider]:
                                disp = normalize_model_name(upstream_name)
                                if disp not in unique_models:
                                    unique_models.add(disp)
                                    model_info = {
                                        "id": disp,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                    else:
                        for provider_item in config["providers"]:
                            if provider_item['provider'] != provider:
                                continue
                            # 跳过禁用的渠道
                            if provider_item.get("enabled") is False:
                                continue
                            # 分组过滤：provider 必须与当前 Key 分组有交集
                            p_groups = provider_item.get("groups") or ["default"]
                            if isinstance(p_groups, str):
                                p_groups = [p_groups] if p_groups else ["default"]
                            if not isinstance(p_groups, list) or not p_groups:
                                p_groups = ["default"]
                            if not allowed_groups.intersection(set(p_groups)):
                                continue

                            model_dict = get_model_dict(provider_item)
                            # 剔除被重定向的上游原名后再进行精确匹配
                            upstream_candidates = {v for k, v in model_dict.items() if v != k}
                            # 如果渠道配置了 model_prefix，只展示带前缀的模型名
                            prefix = provider_item.get('model_prefix', '').strip()
                            for model_item in model_dict.keys():
                                if model_item in upstream_candidates:
                                    continue
                                # 如果有前缀，只返回带前缀的模型名
                                if prefix and not model_item.startswith(prefix):
                                    continue
                                if model_item not in unique_models and model_item == model:
                                    unique_models.add(model_item)
                                    model_info = {
                                        "id": model_item,
                                        "object": "model",
                                        "created": 1720524448858,
                                        "owned_by": "Zoaholic"
                                    }
                                    all_models.append(model_info)
                continue

            if model.startswith("sk-") and model in api_list:
                continue

            disp = normalize_model_name(model)
            if disp not in unique_models:
                unique_models.add(disp)
                model_info = {
                    "id": disp,
                    "object": "model",
                    "created": 1720524448858,
                    "owned_by": "Zoaholic"
                }
                all_models.append(model_info)

    # 最终统一：仍为上游原名的 id 转换为展示别名，并做去重
    final_models = []
    seen = set()
    for item in all_models:
        disp = normalize_model_name(item["id"])
        if disp not in seen:
            seen.add(disp)
            item["id"] = disp
            final_models.append(item)
    return final_models

def get_all_models(config, allowed_groups=None):
    all_models = []
    unique_models = set()
    
    for provider in config["providers"]:
        # 跳过禁用的渠道
        if provider.get("enabled") is False:
            continue
            
        # 分组过滤：如果提供了允许分组集合，需存在交集
        if allowed_groups is not None:
            p_groups = provider.get("groups") or ["default"]
            if isinstance(p_groups, str):
                p_groups = [p_groups] if p_groups else ["default"]
            if not isinstance(p_groups, list) or not p_groups:
                p_groups = ["default"]
            if not allowed_groups.intersection(set(p_groups)):
                continue

        # 使用映射缓存（若不存在则回退到实时计算）
        model_dict = provider.get("_model_dict_cache") or get_model_dict(provider)
        # 识别被重定向的上游原名（出现在映射值中的项且与键不同）
        upstream_candidates = {v for k, v in model_dict.items() if v != k}
        
        # 如果渠道配置了 model_prefix，只展示带前缀的模型名
        prefix = provider.get('model_prefix', '').strip()
        
        for model in model_dict.keys():
            # 仅返回展示别名，过滤掉被重定向的上游原名
            if model in upstream_candidates:
                continue
            # 如果有前缀，只返回带前缀的模型名，过滤掉不带前缀的原始模型名
            if prefix and not model.startswith(prefix):
                continue
            if model not in unique_models:
                unique_models.add(model)
                model_info = {
                    "id": model,
                    "object": "model",
                    "created": 1720524448858,
                    "owned_by": "Zoaholic"
                }
                all_models.append(model_info)
    
    return all_models

async def query_channel_key_stats(
    provider_name: str,
    start_dt: Optional[datetime] = None,
    end_dt: Optional[datetime] = None,
) -> List[Dict]:
    """Queries the ChannelStat table for API key success rates."""
    if DISABLE_DATABASE:
        return []

    async with async_session() as session:
        if not start_dt:
            start_dt = datetime.now(timezone.utc) - timedelta(hours=24)

        query = (
            select(
                ChannelStat.provider_api_key,
                func.count().label("total_requests"),
                func.sum(case((ChannelStat.success, 1), else_=0)).label(
                    "success_count"
                ),
            )
            .where(ChannelStat.provider == provider_name)
            .where(ChannelStat.timestamp >= start_dt)
            .where(ChannelStat.provider_api_key.isnot(None))
        )

        if end_dt:
            query = query.where(ChannelStat.timestamp < end_dt)

        query = query.group_by(ChannelStat.provider_api_key)

        result = await session.execute(query)
        stats_from_db = result.mappings().all()

    key_stats = []
    for row in stats_from_db:
        key_stats.append(
            {
                "api_key": row.provider_api_key,
                "success_count": row.success_count,
                "total_requests": row.total_requests,
                "success_rate": row.success_count / row.total_requests
                if row.total_requests > 0
                else 0,
            }
        )

    # Sort the results by success rate and total requests
    sorted_stats = sorted(
        key_stats,
        key=lambda item: (item["success_rate"], item["total_requests"]),
        reverse=True,
    )

    return sorted_stats


async def get_sorted_api_keys(
    provider_name: str, all_keys_in_config: list, group_size: int = 100
):
    """
    获取根据成功率和特定分组算法排序的API密钥列表。

    1. 从数据库查询过去72小时内各API key的成功和失败次数。
    2. 计算成功率，并对所有key（包括未使用的key）进行排序。
    3. 应用“矩阵转置”分组算法，以平衡负载和探索。
    """
    if not all_keys_in_config:
        return []

    key_stats = {}
    try:
        start_time = datetime.now(timezone.utc) - timedelta(hours=72)
        stats_list = await query_channel_key_stats(provider_name, start_dt=start_time)
        for stat in stats_list:
            key_stats[stat["api_key"]] = {
                "success_rate": stat["success_rate"],
                "total_requests": stat["total_requests"],
            }
    except Exception as e:
        logger.error(
            f"Error querying key stats from DB for provider '{provider_name}': {e}"
        )
        # 在数据库查询失败时，返回原始顺序，确保系统可用性
        return all_keys_in_config

    # 对所有在配置文件中定义的key进行排序
    # 排序规则：1. 成功率降序 2. 总尝试次数降序（成功率相同时，尝试多的更可信）
    # 对于从未用过的key，它们会自然排在最后
    sorted_keys = sorted(
        all_keys_in_config,
        key=lambda k: (
            key_stats.get(k, {"success_rate": -1})["success_rate"],
            key_stats.get(k, {"total_requests": 0})["total_requests"],
        ),
        reverse=True,
    )

    # 应用“矩阵转置”分组算法
    num_keys = len(sorted_keys)
    if num_keys == 0:
        return []

    num_groups = (num_keys + group_size - 1) // group_size
    groups = [[] for _ in range(num_groups)]

    for i, key in enumerate(sorted_keys):
        groups[i % num_groups].append(key)

    final_sorted_list = []
    for group in groups:
        final_sorted_list.extend(group)

    logger.info(
        f"Successfully sorted {len(final_sorted_list)} keys for provider '{provider_name}' using smart algorithm."
    )
    return final_sorted_list
