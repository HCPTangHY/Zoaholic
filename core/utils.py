"""
通用工具入口模块。

修改原因：core.utils.py 原来同时承载基础工具、API key 池、响应构造和图片处理，文件体积过大。
修改方式：将 API key 池移入 core.key_pool，将响应构造和图片处理移入 core.response_builders，并在此处重新导出旧名字。
目的：不改变外部调用方式，继续兼容 from core.utils import ... 的旧导入路径。
"""

import json
import httpx
import traceback
from httpx_socks import AsyncProxyTransport
from urllib.parse import urlparse, urlunparse

from .log_config import logger

# 修改原因：外部代码仍从 core.utils 导入 API key 池相关名字。
# 修改方式：在 utils 顶部重新导出 core.key_pool 中的同一批对象。
# 目的：确保 provider_api_circular_list 全局只有 core.key_pool 中的一个实例。
from core.key_pool import (
    parse_rate_limit, ThreadSafeCircularList, circular_list_encoder,
    ApiKeyRateLimitRegistry, provider_api_circular_list,
    _save_all_auto_disabled, load_auto_disabled_snapshot, restore_auto_disabled,
)
from core.response_builders import (
    end_of_line, generate_sse_response, generate_no_stream_response,
    generate_chunked_image_md, get_image_format, encode_image,
    get_image_from_url, get_encode_image, get_base64_image,
    _convert_webp_base64_to_png, _prepare_image_for_upload,
    upload_image_to_0x0st, parse_json_safely,
)

# 本地 API Key 前缀：用于判断 provider 名是否为本地聚合器 Key
# sk- 是历史前缀，zk- 是新版本前缀，两者都需要兼容
LOCAL_API_KEY_PREFIXES = ("sk-", "zk-")

def is_local_api_key(name: str) -> bool:
    """
    判断一个 provider 名称是否为本地 API Key（聚合器 Key）。
    本地 Key 以 sk-（历史）或 zk-（新版）开头。
    """
    return name.startswith(LOCAL_API_KEY_PREFIXES)



def get_model_dict(provider):

    """
    构建模型别名到上游模型名的映射字典。
    
    YAML 配置格式：
    - 字符串：直接使用，别名和上游都是自己
      例：`- gemini-2.5-pro` → alias="gemini-2.5-pro", upstream="gemini-2.5-pro"
    - 字典：`{upstream: alias}` 格式，key 是上游模型名，value 是对外展示的别名
      例：`- gemini-2.5-pro: my-alias` → upstream="gemini-2.5-pro", alias="my-alias"
    
    如果 provider 配置了 model_prefix，只生成带前缀的对外别名 -> 上游模型。
    无前缀请求是否能命中带前缀渠道，由 routing.py 的 pool_sharing 显式控制。
    这样做的目的，是避免未开启 pool_sharing 的旧渠道被无前缀模型名误命中。
    
    Returns:
        dict: {alias: upstream_model} 映射
    """
    if provider.get("_virtual_route_provider") and isinstance(provider.get("_model_dict_cache"), dict):
        # 修改原因：虚拟模型临时 provider 会保留原渠道的 model_prefix，但请求链路中有部分代码会调用 get_model_dict。
        # 修改方式：对带 _virtual_route_provider 标记的临时 provider，优先返回已注入虚拟名映射的运行时缓存副本。
        # 目的：确保 request.model 为虚拟模型名时，所有渠道实现都能解析到真实上游模型名。
        return dict(provider["_model_dict_cache"])

    # 修改原因：service.update_config 会为每个渠道注入 _model_dict_cache，但热更新中途失败等异常路径会漏注入，
    # 导致下游 provider["_model_dict_cache"] 直接下标时抛 KeyError。
    # 修改方式：get_model_dict 作为统一读取入口，已有缓存时直接返回；缓存缺失时下面照常重算。
    # 目的：让所有调用点改用 get_model_dict 后既能享受缓存，又不会因运行时字段缺失而 500。
    cached_model_dict = provider.get("_model_dict_cache")
    if isinstance(cached_model_dict, dict):
        return cached_model_dict

    model_dict = {}
    prefix = provider.get('model_prefix', '').strip()
    
    if "model" not in provider:
        logger.error(f"Error: model is not set in provider: {provider}")
        return model_dict
        
    for model in provider['model']:
        if isinstance(model, str):
            # 字符串模型：别名和上游都是自己。
            # 修改原因：model_prefix 渠道默认只能暴露带前缀外部名；无前缀共享必须由 pool_sharing 单独开启。
            # 修改方式：普通模型只写入 prefix+model；通配符 "*" 是路由标记，不加前缀，保持既有透传能力。
            if model == "*":
                model_dict[model] = model
            elif prefix:
                model_dict[f"{prefix}{model}"] = model  # 带前缀别名 -> 上游
            else:
                model_dict[model] = model  # 无前缀渠道：原始名 -> 上游
            
        if isinstance(model, dict):
            # dict 模型格式: {upstream: alias}
            # key = 上游模型名
            # value = 对外展示的别名
            for upstream, alias in model.items():
                alias_str = str(alias)
                upstream_str = str(upstream)
                # 修改原因：保持 model_dict 的 key 始终为对外模型名，value 始终为上游原始名。
                # 修改方式：有前缀时只写入带前缀别名；无前缀时写入原别名。
                # 目的：让 pool_sharing 成为无前缀路由池共享的唯一入口。
                if prefix:
                    model_dict[f"{prefix}{alias_str}"] = upstream_str  # 带前缀别名 -> 上游
                else:
                    model_dict[alias_str] = upstream_str  # 无前缀渠道：别名 -> 上游
                
    return model_dict


def resolve_base_url(base_url: str, suffix: str) -> str:
    """解析 base_url 并拼接后缀。

    当 base_url 以 '#' 结尾时，去掉 '#' 后直接使用该地址，不拼接 suffix。
    这允许用户通过在 base_url 末尾加 '#' 来精确指定完整的请求地址。

    示例:
        resolve_base_url("https://example.com/v1", "/chat/completions")
        → "https://example.com/v1/chat/completions"

        resolve_base_url("https://example.com/v10/chat#", "/chat/completions")
        → "https://example.com/v10/chat"
    """
    if base_url.endswith('#'):
        return base_url[:-1].rstrip('/')
    return base_url.rstrip('/') + suffix


class BaseAPI:
    def __init__(
        self,
        api_url: str = "https://api.openai.com/v1/chat/completions",
    ):
        if api_url == "":
            api_url = "https://api.openai.com/v1/chat/completions"

        # 如果 URL 以 '#' 结尾，表示用户希望直接使用该地址，不做任何路径拼接
        if api_url.endswith('#'):
            fixed_url = api_url[:-1].rstrip('/')
            self.source_api_url = fixed_url
            self.base_url = fixed_url
            self.v1_url = fixed_url
            self.v1_models = fixed_url
            self.chat_url = fixed_url
            self.image_url = fixed_url
            self.audio_transcriptions = fixed_url
            self.moderations = fixed_url
            self.embeddings = fixed_url
            self.audio_speech = fixed_url
            return

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
            # path 必须以 / 开头，否则 urlunparse 会生成无效 URL
            self.v1_models: str = urlunparse(parsed_url[:2] + ("/v1/models",) + ("",) * 3)
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

def is_tools_disabled(provider) -> bool:
    """检查 provider 是否禁用了工具调用"""
    # 修改原因：工具配置现在只保留禁用开关，避免模式判断截断工具调用。
    # 修改方式：只有 provider.tools 显式为 False 时才返回 True。
    # 目的：保留禁用工具功能，同时让已经生成的工具调用完整转发。
    return provider.get("tools") is False


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

    # stream_mode: auto(默认) / force_stream / force_non_stream
    stream_mode = safe_get(provider, "preferences", "stream_mode", default="auto")

    return engine, stream, stream_mode

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
            client_config["proxy"] = proxy
    return client_config

async def update_initial_model(provider):
    try:
        engine, stream_mode, _ = get_engine(provider, endpoint=None, original_model="")
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
            # 若大体积 JSON 字符串（>256KB，如含 Base64 图片的请求/响应）中夹带超长字面量，
            # 先用 C 正则原地截断 >4096 字符的巨型字符串字面量，避免全量 json.loads 展开 10MB+ AST 树
            if len(data) > 256 * 1024 and not data.lstrip().startswith('data: '):
                import re as _re_trunc
                _ms = max_str_length
                data = _re_trunc.sub(
                    r'"([^"\\]{4096,})"',
                    lambda m: f'"{m.group(1)[:_ms]}... [截断 {len(m.group(1)) - _ms} 字符]"',
                    data,
                )
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
