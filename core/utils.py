import re
import io
import ast
import json
import httpx
import base64
import random
import string
import asyncio
import traceback
from time import time
from PIL import Image
from fastapi import HTTPException
from collections import defaultdict
from httpx_socks import AsyncProxyTransport
from urllib.parse import urlparse, urlunparse

from .log_config import logger

def get_model_dict(provider):
    """
    构建模型别名到上游模型名的映射字典。
    
    YAML 配置格式：
    - 字符串：直接使用，别名和上游都是自己
      例：`- gemini-2.5-pro` → alias="gemini-2.5-pro", upstream="gemini-2.5-pro"
    - 字典：`{upstream: alias}` 格式，key 是上游模型名，value 是对外展示的别名
      例：`- gemini-2.5-pro: my-alias` → upstream="gemini-2.5-pro", alias="my-alias"
    
    如果 provider 配置了 model_prefix，会同时生成：
    - 带前缀的别名 -> 上游模型（用于模型列表展示和带前缀请求匹配）
    - 不带前缀的别名 -> 上游模型（用于不带前缀请求的路由匹配）
    
    Returns:
        dict: {alias: upstream_model} 映射
    """
    model_dict = {}
    prefix = provider.get('model_prefix', '').strip()
    
    if "model" not in provider:
        logger.error(f"Error: model is not set in provider: {provider}")
        return model_dict
        
    for model in provider['model']:
        if isinstance(model, str):
            # 字符串模型：别名和上游都是自己
            if prefix:
                model_dict[f"{prefix}{model}"] = model  # 带前缀别名 -> 上游
            model_dict[model] = model  # 原始名 -> 上游（用于路由匹配）
            
        if isinstance(model, dict):
            # dict 模型格式: {upstream: alias}
            # key = 上游模型名
            # value = 对外展示的别名
            for upstream, alias in model.items():
                alias_str = str(alias)
                upstream_str = str(upstream)
                if prefix:
                    model_dict[f"{prefix}{alias_str}"] = upstream_str  # 带前缀别名 -> 上游
                model_dict[alias_str] = upstream_str  # 原始别名 -> 上游（用于路由匹配）
                
    return model_dict

class BaseAPI:
    def __init__(
        self,
        api_url: str = "https://api.openai.com/v1/chat/completions",
    ):
        if api_url == "":
            api_url = "https://api.openai.com/v1/chat/completions"
        self.source_api_url: str = api_url
        parsed_url = urlparse(self.source_api_url)
        # print("parsed_url", parsed_url)
        if parsed_url.scheme == "":
            raise Exception("Error: API_URL is not set")
        if parsed_url.path != '/':
            before_v1 = parsed_url.path.split("chat/completions")[0]
            if not before_v1.endswith("/"):
                before_v1 = before_v1 + "/"
        else:
            before_v1 = ""
        self.base_url: str = urlunparse(parsed_url[:2] + ("",) + ("",) * 3)
        self.v1_url: str = urlunparse(parsed_url[:2]+ (before_v1,) + ("",) * 3)
        if "v1/messages" in parsed_url.path:
            self.v1_models: str = urlunparse(parsed_url[:2] + ("v1/models",) + ("",) * 3)
        else:
            self.v1_models: str = urlunparse(parsed_url[:2] + (before_v1 + "models",) + ("",) * 3)

        if "v1/responses" in parsed_url.path:
            self.chat_url: str = api_url
        else:
            self.chat_url: str = urlunparse(parsed_url[:2] + (before_v1 + "chat/completions",) + ("",) * 3)
        self.image_url: str = urlunparse(parsed_url[:2] + (before_v1 + "images/generations",) + ("",) * 3)
        if parsed_url.hostname == "dashscope.aliyuncs.com":
            self.audio_transcriptions: str = urlunparse(parsed_url[:2] + ("/api/v1/services/aigc/multimodal-generation/generation",) + ("",) * 3)
        else:
            self.audio_transcriptions: str = urlunparse(parsed_url[:2] + (before_v1 + "audio/transcriptions",) + ("",) * 3)
        self.moderations: str = urlunparse(parsed_url[:2] + (before_v1 + "moderations",) + ("",) * 3)
        self.embeddings: str = urlunparse(parsed_url[:2] + (before_v1 + "embeddings",) + ("",) * 3)
        if parsed_url.hostname == "api.minimaxi.com":
            self.audio_speech: str = urlunparse(parsed_url[:2] + ("v1/t2a_v2",) + ("",) * 3)
        else:
            self.audio_speech: str = urlunparse(parsed_url[:2] + (before_v1 + "audio/speech",) + ("",) * 3)

        if parsed_url.path.endswith("/v1beta") or \
        (parsed_url.netloc == 'generativelanguage.googleapis.com' and "openai/chat/completions" not in parsed_url.path):
            before_v1 = parsed_url.path.split("/v1")[0]
            self.base_url = api_url
            self.v1_url = api_url
            self.chat_url = api_url
            self.embeddings = urlunparse(parsed_url[:2] + (before_v1 + "/v1beta/embeddings",) + ("",) * 3)

def get_tools_mode(provider) -> str:
    """
    获取工具调用支持模式

    Args:
        provider: provider 配置

    Returns:
        str: 工具模式
            - "none": 不支持工具调用
            - "single": 只支持单个工具调用（默认）
            - "parallel": 支持并行工具调用
    """
    tools_config = provider.get("tools")

    if tools_config is False:
        return "none"
    elif tools_config == "parallel":
        return "parallel"
    elif tools_config is True or tools_config == "single":
        return "single"
    elif tools_config is None:
        # 未配置时默认为 single（向后兼容）
        return "single"
    else:
        # 其他值视为 single
        return "single"


def get_engine(provider, endpoint=None, original_model=""):
    """
    获取引擎类型和流式模式
    
    Args:
        provider: provider 配置，必须包含 engine 字段
        endpoint: 请求端点（可选）
        original_model: 原始模型名（可选）
        
    Returns:
        tuple: (engine, stream)
        
    Raises:
        ValueError: 当 provider 未配置 engine 字段时
    """
    stream = None
    
    # 强制要求配置 engine 字段
    engine = provider.get("engine")
    if not engine:
        raise ValueError(
            f"provider 必须配置 engine 字段。"
        )
    
    # 处理 vertex 的子类型区分（同一平台不同 API 格式）
    original_model_lower = original_model.lower() if original_model else ""
    if engine == "vertex":
        if "claude" in original_model_lower:
            engine = "vertex-claude"
        else:
            engine = "vertex-gemini"

    # 允许通过配置覆盖 stream 模式
    if "stream" in safe_get(provider, "preferences", "post_body_parameter_overrides", default={}):
        stream = safe_get(provider, "preferences", "post_body_parameter_overrides", "stream")

    return engine, stream

def get_proxy(proxy, client_config = {}):
    if proxy:
        # 解析代理URL
        parsed = urlparse(proxy)
        scheme = parsed.scheme.rstrip('h')

        if scheme == 'socks5':
            proxy = proxy.replace('socks5h://', 'socks5://')
            transport = AsyncProxyTransport.from_url(proxy)
            client_config["transport"] = transport
            # print("proxy", proxy)
        else:
            client_config["proxies"] = {
                "http://": proxy,
                "https://": proxy
            }
    return client_config

async def update_initial_model(provider):
    try:
        engine, stream_mode = get_engine(provider, endpoint=None, original_model="")
        # print("engine", engine, provider)
        api_url = provider['base_url']
        api = provider['api']
        proxy = safe_get(provider, "preferences", "proxy", default=None)
        client_config = get_proxy(proxy)
        if engine == "gemini":
            before_v1 = api_url.split("/v1beta")[0]
            url = before_v1 + "/v1beta/models"
            params = {"key": api}
            async with httpx.AsyncClient(**client_config) as client:
                response = await client.get(url, params=params)

            original_models = response.json()
            if original_models.get("error"):
                raise Exception({"error": original_models.get("error"), "endpoint": url, "api": api})

            models = {"data": []}
            for model in original_models["models"]:
                models["data"].append({
                    "id": model["name"].split("models/")[-1],
                })
        else:
            endpoint = BaseAPI(api_url=api_url)
            endpoint_models_url = endpoint.v1_models
            if isinstance(api, list):
                api = api[0]
            if "v1/messages" in api_url:
                headers = {"x-api-key": api, "anthropic-version": "2023-06-01"}
            else:
                headers = {"Authorization": f"Bearer {api}"}
            async with httpx.AsyncClient(**client_config) as client:
                response = await client.get(
                    endpoint_models_url,
                    headers=headers,
                )
            models = response.json()
            if models.get("error"):
                logger.error({"error": models.get("error"), "endpoint": endpoint_models_url, "api": api})
                return []

        # print(models)
        models_list = models["data"]
        models_id = [model["id"] for model in models_list]
        set_models = set()
        for model_item in models_id:
            set_models.add(model_item)
        models_id = list(set_models)
        # print(models_id)
        return models_id
    except Exception:
        traceback.print_exc()
        return []

def safe_get(data, *keys, default=None):
    for key in keys:
        try:
            if isinstance(data, (dict, list)):
                data = data[key]
            elif isinstance(key, str) and hasattr(data, key):
                data = getattr(data, key)
            else:
                data = data.get(key)
        except (KeyError, IndexError, AttributeError, TypeError):
            return default
    if not data:
        return default
    return data



def truncate_for_logging(
    data,
    max_total_size: int = 100 * 1024,
    max_str_length: int = 2000,
    max_items: int = 50,
    max_depth: int = 8,
):
    """
    深度遍历并截断日志数据：保留结构，限制单项长度/数量/深度。

    - 字符串超过 max_str_length 进行截断并标注剩余长度
    - list/dict 超过 max_items 仅保留前 max_items 项并标注剩余
    - 深度超过 max_depth 返回占位说明
    - 最终序列化后若总长度超过 max_total_size 进行总长度截断
    """

    def _truncate(obj, depth):
        if depth >= max_depth:
            return "[已截断：已达到最大深度]"

        if isinstance(obj, str):
            if len(obj) > max_str_length:
                return obj[:max_str_length] + f"... [截断 {len(obj) - max_str_length} 字符]"
            return obj

        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj

        if isinstance(obj, dict):
            truncated_dict = {}
            for idx, (k, v) in enumerate(obj.items()):
                if idx >= max_items:
                    truncated_dict["__truncated_keys__"] = f"[{len(obj) - max_items} 更多项]"
                    break
                key_str = k if isinstance(k, str) else str(k)
                truncated_dict[key_str] = _truncate(v, depth + 1)
            return truncated_dict

        if isinstance(obj, list):
            truncated_list = []
            for idx, item in enumerate(obj):
                if idx >= max_items:
                    truncated_list.append(f"[... {len(obj) - max_items} 更多项]")
                    break
                truncated_list.append(_truncate(item, depth + 1))
            return truncated_list

        return str(obj)

    def _truncate_sse(text):
        """处理 SSE 格式的流式响应，对每个事件的 JSON 内部进行截断"""
        lines = text.replace('\r\n', '\n').split('\n')
        result_lines = []
        
        for line in lines:
            if line.startswith('data: '):
                data_str = line[6:]  # 去掉 "data: " 前缀
                if data_str == '[DONE]':
                    result_lines.append(line)
                else:
                    try:
                        parsed = json.loads(data_str)
                        truncated = _truncate(parsed, 0)
                        result_lines.append('data: ' + json.dumps(truncated, ensure_ascii=False))
                    except Exception:
                        # 解析失败，保留原始行
                        result_lines.append(line)
            else:
                # 非 data: 行（空行、注释、event: 等）保留原样
                result_lines.append(line)
        
        return '\n'.join(result_lines)

    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", errors="replace")

        if isinstance(data, str):
            # 检测是否是 SSE 格式（以 "data: " 开头）
            stripped = data.strip()
            if stripped.startswith('data: '):
                # SSE 流式响应格式，对每个事件块内部进行截断
                serialized = _truncate_sse(data)
            else:
                try:
                    parsed = json.loads(data)
                    truncated_obj = _truncate(parsed, 0)
                    serialized = json.dumps(truncated_obj, ensure_ascii=False)
                except Exception:
                    truncated_obj = _truncate(data, 0)
                    serialized = json.dumps(truncated_obj, ensure_ascii=False)
        else:
            truncated_obj = _truncate(data, 0)
            serialized = json.dumps(truncated_obj, ensure_ascii=False)
    except Exception:
        serialized = str(data)

    if len(serialized) > max_total_size:
        serialized = serialized[:max_total_size] + f"... [截断总计 {len(serialized) - max_total_size} 字符]"

    return serialized


def parse_rate_limit(limit_string):
    # 定义时间单位到秒的映射
    time_units = {
        's': 1, 'sec': 1, 'second': 1,
        'm': 60, 'min': 60, 'minute': 60,
        'h': 3600, 'hr': 3600, 'hour': 3600,
        'd': 86400, 'day': 86400,
        'mo': 2592000, 'month': 2592000,
        'y': 31536000, 'year': 31536000,
        'tpr': -1,
    }

    # 处理多个限制条件
    limits = []
    for limit in limit_string.split(','):
        limit = limit.strip()
        # 使用正则表达式匹配数字和单位
        match = re.match(r'^(\d+)/(\w+)$', limit)
        if not match:
            raise ValueError(f"Invalid rate limit format: {limit}")

        count, unit = match.groups()
        count = int(count)

        # 转换单位到秒
        if unit not in time_units:
            raise ValueError(f"Unknown time unit: {unit}")

        seconds = time_units[unit]
        limits.append((count, seconds))

    return limits

class ThreadSafeCircularList:
    def __init__(self, items = [], rate_limit={"default": "999999/min"}, schedule_algorithm="round_robin", provider_name=None, disabled_keys=None):
        self.provider_name = provider_name
        self.original_items = list(items)
        self.schedule_algorithm = schedule_algorithm
        # 存储禁用的 key 集合
        self.disabled_keys = set(disabled_keys) if disabled_keys else set()

        if schedule_algorithm == "random":
            self.items = random.sample(items, len(items))
        elif schedule_algorithm == "round_robin":
            self.items = items
        elif schedule_algorithm == "fixed_priority":
            self.items = items
        elif schedule_algorithm == "smart_round_robin":
            self.items = items
        else:
            self.items = items
            logger.warning(f"Unknown schedule algorithm: {schedule_algorithm}, use (round_robin, random, fixed_priority, smart_round_robin) instead")
            self.schedule_algorithm = "round_robin"

        self.index = 0
        self.lock = asyncio.Lock()
        self.requests = defaultdict(lambda: defaultdict(list))
        self.cooling_until = defaultdict(float)
        self.rate_limits = {}
        self.reordering_task = None

        if isinstance(rate_limit, dict):
            for rate_limit_model, rate_limit_value in rate_limit.items():
                self.rate_limits[rate_limit_model] = parse_rate_limit(rate_limit_value)
        elif isinstance(rate_limit, str):
            self.rate_limits["default"] = parse_rate_limit(rate_limit)
        else:
            logger.error(f"Error ThreadSafeCircularList: Unknown rate_limit type: {type(rate_limit)}, rate_limit: {rate_limit}")

        if self.schedule_algorithm == "smart_round_robin":
            logger.info(f"Initializing '{self.provider_name}' with 'smart_round_robin' algorithm.")
            self._trigger_reorder()

    async def reset_items(self, new_items: list):
        """Safely replaces the current list of items with a new one."""
        async with self.lock:
            if self.items != new_items:
                self.items = new_items
                self.index = 0
                logger.info(f"Provider '{self.provider_name}' API key list has been reset and reordered.")

    def _trigger_reorder(self):
        """Asynchronously triggers the reordering task if not already running."""
        if self.provider_name and (self.reordering_task is None or self.reordering_task.done()):
            logger.info(f"Triggering reorder for provider '{self.provider_name}'...")
            try:
                loop = asyncio.get_running_loop()
                self.reordering_task = loop.create_task(self._reorder_keys())
            except RuntimeError:
                logger.warning(f"No running event loop to trigger reorder for '{self.provider_name}'.")

    async def _reorder_keys(self):
        """Performs the actual reordering logic."""
        from utils import get_sorted_api_keys
        try:
            sorted_keys = await get_sorted_api_keys(self.provider_name, self.original_items, group_size=100)
            if sorted_keys:
                await self.reset_items(sorted_keys)
        except Exception as e:
            logger.error(f"Error during key reordering for provider '{self.provider_name}': {e}")

    async def set_cooling(self, item: str, cooling_time: int = 60):
        """设置某个 item 进入冷却状态

        Args:
            item: 需要冷却的 item
            cooling_time: 冷却时间(秒)，默认60秒
        """
        if item is None:
            return
        now = time()
        async with self.lock:
            self.cooling_until[item] = now + cooling_time
            # 清空该 item 的请求记录
            # self.requests[item] = []
            logger.warning(f"API key {item} 已进入冷却状态，冷却时间 {cooling_time} 秒")

    def is_key_disabled(self, item: str) -> bool:
        """检查某个 key 是否被禁用
        
        Args:
            item: API key
            
        Returns:
            bool: 如果 key 被禁用返回 True，否则返回 False
        """
        return item in self.disabled_keys
    
    def set_key_disabled(self, item: str, disabled: bool = True):
        """设置某个 key 的禁用状态
        
        Args:
            item: API key
            disabled: True 表示禁用，False 表示启用
        """
        if disabled:
            self.disabled_keys.add(item)
        else:
            self.disabled_keys.discard(item)
    
    def update_disabled_keys(self, disabled_keys: set):
        """更新禁用的 key 集合
        
        Args:
            disabled_keys: 新的禁用 key 集合
        """
        self.disabled_keys = set(disabled_keys) if disabled_keys else set()

    async def is_rate_limited(self, item, model: str = None, is_check: bool = False) -> bool:
        now = time()
        # 检查是否被禁用
        if self.is_key_disabled(item):
            return True
        # 检查是否在冷却中
        if now < self.cooling_until[item]:
            return True

        # 获取适用的速率限制

        if model:
            model_key = model
        else:
            model_key = "default"

        rate_limit = None
        # 先尝试精确匹配
        if model and model in self.rate_limits:
            rate_limit = self.rate_limits[model]
        else:
            # 如果没有精确匹配，尝试模糊匹配
            for limit_model in self.rate_limits:
                if limit_model != "default" and model and limit_model in model:
                    rate_limit = self.rate_limits[limit_model]
                    break

        # 如果都没匹配到，使用默认值
        if rate_limit is None:
            rate_limit = self.rate_limits.get("default", [(999999, 60)])  # 默认限制

        # 检查所有速率限制条件
        for limit_count, limit_period in rate_limit:
            # 使用特定模型的请求记录进行计算
            recent_requests = sum(1 for req in self.requests[item][model_key] if req > now - limit_period)
            if recent_requests >= limit_count:
                if not is_check:
                    logger.warning(f"API key {item}: model: {model_key} has been rate limited ({limit_count}/{limit_period} seconds)")
                return True

        # 清理太旧的请求记录
        max_period = max(period for _, period in rate_limit)
        self.requests[item][model_key] = [req for req in self.requests[item][model_key] if req > now - max_period]

        # 记录新的请求
        if not is_check:
            self.requests[item][model_key].append(now)

        return False

    async def next(self, model: str = None):
        async with self.lock:
            if self.schedule_algorithm == "fixed_priority":
                self.index = 0

            # 检查是否即将完成一个循环，并据此触发重排序
            if self.schedule_algorithm == "smart_round_robin" and len(self.items) > 0 and self.index == len(self.items) - 1:
                self._trigger_reorder()

            start_index = self.index
            while True:
                item = self.items[self.index]
                self.index = (self.index + 1) % len(self.items)

                if not await self.is_rate_limited(item, model):
                    return item

                # 如果已经检查了所有的 API key 都被限制
                if self.index == start_index:
                    logger.warning("All API keys are rate limited!")
                    raise HTTPException(status_code=429, detail="Too many requests")

    async def is_tpr_exceeded(self, model: str = None, tokens: int = 0) -> bool:
        """Checks if the request exceeds the TPR (Tokens Per Request) limit."""
        if not tokens:
            return False

        async with self.lock:
            rate_limit = None
            model_key = model or "default"
            if model and model_key in self.rate_limits:
                rate_limit = self.rate_limits[model_key]
            else:
                # fuzzy match
                for limit_model in self.rate_limits:
                    if limit_model != "default" and model and limit_model in model:
                        rate_limit = self.rate_limits[limit_model]
                        break
            if rate_limit is None:
                rate_limit = self.rate_limits.get("default", [])

            for limit_count, limit_period in rate_limit:
                if limit_period == -1:  # TPR limit
                    if tokens > limit_count:
                        # logger.warning(f"API provider for model {model_key} exceeds TPR limit ({tokens}/{limit_count}).")
                        return True
        return False

    async def is_all_rate_limited(self, model: str = None) -> bool:
        """检查是否所有的items都被速率限制

        与next方法不同，此方法不会改变任何内部状态（如self.index），
        仅返回一个布尔值表示是否所有的key都被限制。

        Args:
            model: 要检查的模型名称，默认为None

        Returns:
            bool: 如果所有items都被速率限制返回True，否则返回False
        """
        if len(self.items) == 0:
            return False

        async with self.lock:
            for item in self.items:
                # 跳过禁用的 key
                if self.is_key_disabled(item):
                    continue
                if not await self.is_rate_limited(item, model, is_check=True):
                    return False

            # 如果遍历完所有items都被限制，返回True
            # logger.debug(f"Check result: all items are rate limited!")
            return True
    
    def get_enabled_items_count(self) -> int:
        """返回启用的项目数量
        
        Returns:
            int: 启用的 items 数量
        """
        return len([item for item in self.items if not self.is_key_disabled(item)])

    async def after_next_current(self):
        # 返回当前取出的 API，因为已经调用了 next，所以当前API应该是上一个
        if len(self.items) == 0:
            return None
        async with self.lock:
            item = self.items[(self.index - 1) % len(self.items)]
            return item

    def get_items_count(self) -> int:
        """返回列表中的项目数量

        Returns:
            int: items列表的长度
        """
        return len(self.items)

def circular_list_encoder(obj):
    if isinstance(obj, ThreadSafeCircularList):
        return obj.to_dict()
    raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')

provider_api_circular_list = defaultdict(ThreadSafeCircularList)


# end_of_line = "\n\r\n"
# end_of_line = "\r\n"
# end_of_line = "\n\r"
end_of_line = "\n\n"
# end_of_line = "\r"
# end_of_line = "\n"

async def generate_sse_response(timestamp, model, content=None, tools_id=None, function_call_name=None, function_call_content=None, role=None, total_tokens=0, prompt_tokens=0, completion_tokens=0, reasoning_content=None, stop=None, thought_signature=None):
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))

    delta_content = {"role": "assistant", "content": content} if content else {}
    if reasoning_content:
        delta_content = {"role": "assistant", "content": "", "reasoning_content": reasoning_content}
    
    # 注入签名信息到 delta
    if thought_signature:
        delta_content["thought_signature"] = thought_signature

    sample_data = {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion.chunk",
        "created": timestamp,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta_content,
                "logprobs": None,
                "finish_reason": None if content or reasoning_content else "stop"
            }
        ],
        "usage": None,
        "system_fingerprint": "fp_d576307f90",
    }
    if function_call_content:
        sample_data["choices"][0]["delta"] = {"tool_calls":[{"index":0,"function":{"arguments": function_call_content}}]}
    if tools_id and function_call_name:
        tc = {"index": 0, "id": tools_id, "type": "function", "function": {"name": function_call_name, "arguments": ""}}
        if thought_signature:
            tc["extra_content"] = {"google": {"thoughtSignature": thought_signature}}
        sample_data["choices"][0]["delta"] = {"tool_calls": [tc]}
    if role:
        sample_data["choices"][0]["delta"] = {"role": role, "content": ""}
    if total_tokens:
        sample_data["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}
        sample_data["choices"] = []
    if stop:
        sample_data["choices"][0]["delta"] = {}
        sample_data["choices"][0]["finish_reason"] = stop

    json_data = await asyncio.to_thread(json.dumps, sample_data, ensure_ascii=False)
    # print("json_data", json.dumps(sample_data, indent=4, ensure_ascii=False))

    # 构建SSE响应
    sse_response = f"data: {json_data}" + end_of_line

    return sse_response

async def generate_no_stream_response(timestamp, model, content=None, tools_id=None, function_call_name=None, function_call_content=None, role=None, total_tokens=0, prompt_tokens=0, completion_tokens=0, reasoning_content=None, image_base64=None, thought_signature=None):
    random.seed(timestamp)
    random_str = ''.join(random.choices(string.ascii_letters + string.digits, k=29))
    message = {
        "role": role,
        "content": content,
        "refusal": None
    }
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    
    if thought_signature:
        message["thought_signature"] = thought_signature

    sample_data = {
        "id": f"chatcmpl-{random_str}",
        "object": "chat.completion",
        "created": timestamp,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": "stop"
            }
        ],
        "usage": None,
        "system_fingerprint": "fp_a7d06e42a7"
    }

    if function_call_name:
        if not tools_id:
            tools_id = f"call_{random_str}"

        arguments_json = await asyncio.to_thread(json.dumps, function_call_content, ensure_ascii=False)

        sample_data = {
            "id": f"chatcmpl-{random_str}",
            "object": "chat.completion",
            "created": timestamp,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tools_id,
                            "type": "function",
                            "function": {
                                "name": function_call_name,
                                "arguments": arguments_json
                            },
                            "extra_content": {"google": {"thoughtSignature": thought_signature}} if thought_signature else None
                        }
                    ],
                    "refusal": None
                    },
                    "logprobs": None,
                    "finish_reason": "tool_calls"
                }
            ],
            "usage": None,
            "service_tier": "default",
            "system_fingerprint": "fp_4691090a87"
        }

    if image_base64:
        sample_data = {
            "created": timestamp,
            "data": [{
                "b64_json": image_base64
            }],
            # "usage": {
            #     "total_tokens": 100,
            #     "input_tokens": 50,
            #     "output_tokens": 50,
            # }
        }

    if total_tokens:
        sample_data["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens}

    json_data = await asyncio.to_thread(json.dumps, sample_data, ensure_ascii=False)
    # print("json_data", json.dumps(sample_data, indent=4, ensure_ascii=False))

    return json_data

def get_image_format(file_content: bytes):
    try:
        img = Image.open(io.BytesIO(file_content))
        return img.format.lower()
    except Exception:
        return None

def encode_image(file_content: bytes):
    img_format = get_image_format(file_content)
    if not img_format:
        raise ValueError("无法识别的图片格式")
    base64_encoded = base64.b64encode(file_content).decode('utf-8')

    if img_format == 'png':
        return f"data:image/png;base64,{base64_encoded}"
    elif img_format in ['jpg', 'jpeg']:
        return f"data:image/jpeg;base64,{base64_encoded}"
    else:
        raise ValueError(f"不支持的图片格式: {img_format}")

async def get_image_from_url(url):
    transport = httpx.AsyncHTTPTransport(
        http2=True,
        verify=False,
        retries=1
    )
    async with httpx.AsyncClient(transport=transport) as client:
        try:
            response = await client.get(
                url,
                timeout=30.0
            )
            response.raise_for_status()
            return response.content

        except httpx.RequestError as e:
            logger.error(f"请求 URL 时出错 {e.request.url!r}: {e}")
            raise HTTPException(status_code=400, detail=f"无法从 URL 获取内容: {url}")
        except httpx.HTTPStatusError as e:
            logger.error(f"获取 URL 时发生 HTTP 错误 {e.request.url!r}: {e.response.status_code}")
            raise HTTPException(status_code=e.response.status_code, detail=f"获取 URL 时出错: {url}")

async def get_encode_image(image_url):
    file_content = await get_image_from_url(image_url)
    base64_image = encode_image(file_content)
    return base64_image

# from PIL import Image
# import io
# def validate_image(image_data, image_type):
#     try:
#         decoded_image = base64.b64decode(image_data)
#         image = Image.open(io.BytesIO(decoded_image))

#         # 检查图片格式是否与声明的类型匹配
#         # print("image.format", image.format)
#         if image_type == "image/png" and image.format != "PNG":
#             raise ValueError("Image is not a valid PNG")
#         elif image_type == "image/jpeg" and image.format not in ["JPEG", "JPG"]:
#             raise ValueError("Image is not a valid JPEG")

#         # 如果没有异常,则图片有效
#         return True
#     except Exception as e:
#         print(f"Image validation failed: {str(e)}")
#         return False

async def get_base64_image(image_url: str) -> tuple[str, str]:
    """
    获取 base64 编码的图片数据和 MIME 类型
    
    Args:
        image_url: 图片 URL 或已编码的 base64 字符串
        
    Returns:
        tuple: (base64_image_with_prefix, mime_type)
               例如: ("data:image/png;base64,xxx", "image/png")
    """
    if image_url.startswith("http"):
        base64_image = await get_encode_image(image_url)
    else:
        base64_image = image_url
        
    colon_index = base64_image.index(":")
    semicolon_index = base64_image.index(";")
    image_type = base64_image[colon_index + 1:semicolon_index]

    # 将 webp 转换为 png（某些 API 不支持 webp）
    if image_type == "image/webp":
        image_data = base64.b64decode(base64_image.split(",")[1])
        image = Image.open(io.BytesIO(image_data))
        png_buffer = io.BytesIO()
        image.save(png_buffer, format="PNG")
        png_base64 = base64.b64encode(png_buffer.getvalue()).decode('utf-8')
        base64_image = f"data:image/png;base64,{png_base64}"
        image_type = "image/png"

    return base64_image, image_type

def parse_json_safely(json_str):
    """
    尝试解析JSON字符串，先使用ast.literal_eval，失败则使用json.loads

    Args:
        json_str: 要解析的JSON字符串

    Returns:
        解析后的Python对象

    Raises:
        Exception: 当两种方法都失败时抛出异常
    """
    try:
        # 首先尝试使用ast.literal_eval解析
        return ast.literal_eval(json_str)
    except (SyntaxError, ValueError):
        try:
            # 如果失败，尝试使用json.loads解析
            return json.loads(json_str, strict=False)
        except json.JSONDecodeError as e:
            # 两种方法都失败，抛出异常
            raise Exception(f"无法解析JSON字符串: {e}, {json_str}")

async def upload_image_to_0x0st(base64_image: str):
    """
    Uploads a base64 encoded image to freeimage.host.
    
    Uses freeimage.host public guest API (no API key registration required).
    API docs: https://freeimage.host/page/api

    Args:
        base64_image: The base64 encoded image string.

    Returns:
        The URL of the uploaded image, or original base64_image if upload fails.
    """
    # 提取纯 base64 数据（去除 data URI 前缀，如 "data:image/png;base64,"）
    if "," in base64_image:
        base64_data = base64_image.split(",")[1]
    else:
        base64_data = base64_image

    # freeimage.host 公共 guest API key
    FREEIMAGE_API_KEY = "6d207e02198a847aa98d0a2a901485a5"
    
    async with httpx.AsyncClient() as client:
        try:
            # freeimage.host API: POST https://freeimage.host/api/1/upload
            # 参数: key (API key), source (base64 或文件), format (json/redirect/txt)
            response = await client.post(
                "https://freeimage.host/api/1/upload",
                data={
                    'key': FREEIMAGE_API_KEY,
                    'source': base64_data,
                    'format': 'json',
                },
                timeout=30.0
            )
            response.raise_for_status()
            result = response.json()
            if result.get('success') or result.get('status_code') == 200:
                # 返回直接图片链接
                url = result.get('image', {}).get('url')
                if url:
                    return url
            logger.error(f"freeimage.host 上传失败: {result}")
        except httpx.RequestError as e:
            logger.error(f"请求 freeimage.host 时出错: {e}")
        except httpx.HTTPStatusError as e:
            logger.error(f"上传图片到 freeimage.host 时发生 HTTP 错误: {e.response.status_code}, {e.response.text}")
        except Exception as e:
            logger.error(f"freeimage.host 上传异常: {e}")
    
    # 上传失败，返回原始 base64
    return base64_image

if __name__ == "__main__":
    provider = {
        "base_url": "https://gateway.ai.cloudflare.com/v1/%7Baccount_id%7D/%7Bgateway_id%7D/google-vertex-ai",
        "engine": "vertex",
    }
    print(get_engine(provider))
