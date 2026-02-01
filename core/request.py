"""
请求构建模块

负责根据不同引擎类型构建 API 请求的 URL、headers 和 payload
所有渠道通过 channels 模块的注册中心获取适配器
"""

from .models import RequestModel, Message
from .utils import (
    get_engine,
    get_model_dict,
    safe_get,
)
from .plugins.interceptors import apply_request_interceptors


def _prepend_system_prompt(request: RequestModel, system_prompt: str) -> RequestModel:
    """
    将渠道配置的系统提示词追加到请求消息的最前面
    
    Args:
        request: 原始请求模型
        system_prompt: 渠道配置的系统提示词
        
    Returns:
        RequestModel: 修改后的请求模型（如果有系统提示词则追加，否则返回原请求）
    """
    if system_prompt is None:
        return request

    system_prompt_text = system_prompt if isinstance(system_prompt, str) else str(system_prompt)
    if not system_prompt_text.strip():
        return request
    
    # 检查请求消息中是否已有 system 角色的消息
    has_system_message = any(msg.role == "system" for msg in request.messages)
    
    if has_system_message:
        # 如果已有 system 消息，将渠道提示词追加到第一个 system 消息的内容前面
        new_messages = []
        system_prepended = False
        for msg in request.messages:
            if msg.role == "system" and not system_prepended:
                # 处理 content 可能是字符串或列表的情况
                if isinstance(msg.content, str):
                    new_content = f"{system_prompt}\n\n{msg.content}" if msg.content else system_prompt
                elif isinstance(msg.content, list):
                    # 如果是列表格式，在第一个文本项前追加
                    new_content = list(msg.content)  # 复制列表
                    for i, item in enumerate(new_content):
                        if hasattr(item, 'type') and item.type == "text" and item.text:
                            from .models import ContentItem
                            new_content[i] = ContentItem(
                                type="text",
                                text=f"{system_prompt}\n\n{item.text}"
                            )
                            break
                    else:
                        # 如果没有找到文本项，在列表开头插入一个
                        from .models import ContentItem
                        new_content.insert(0, ContentItem(type="text", text=system_prompt))
                else:
                    new_content = system_prompt
                
                new_msg = Message(
                    role=msg.role,
                    name=msg.name,
                    content=new_content,
                    tool_calls=msg.tool_calls,
                    tool_call_id=msg.tool_call_id
                )
                new_messages.append(new_msg)
                system_prepended = True
            else:
                new_messages.append(msg)
        
        # 创建新的请求对象
        request_dict = request.model_dump(exclude={'messages'})
        request_dict['messages'] = [m.model_dump() if hasattr(m, 'model_dump') else m for m in new_messages]
        return RequestModel(**request_dict)
    else:
        # 如果没有 system 消息，在消息列表开头插入一个新的 system 消息
        system_message = Message(role="system", content=system_prompt)
        new_messages = [system_message] + list(request.messages)
        
        # 创建新的请求对象
        request_dict = request.model_dump(exclude={'messages'})
        request_dict['messages'] = [m.model_dump() if hasattr(m, 'model_dump') else m for m in new_messages]
        return RequestModel(**request_dict)


async def get_payload(request: RequestModel, engine, provider, api_key=None):
    """
    通过渠道注册中心获取请求适配器并构建 payload
    
    Args:
        request: 请求模型
        engine: 引擎类型 (openai, gemini, claude, azure, aws, vertex-gemini, vertex-claude, openrouter, cloudflare)
        provider: 提供商配置
        api_key: API 密钥
        
    Returns:
        tuple: (url, headers, payload)
    """
    from .channels import get_channel
    
    # 检查渠道是否配置了系统提示词，如果有则追加到请求中
    channel_system_prompt = safe_get(provider, "preferences", "system_prompt", default=None)
    if channel_system_prompt:
        request = _prepend_system_prompt(request, channel_system_prompt)
     
    channel = get_channel(engine)
    
    # 如果 provider 的 base_url 为空，使用渠道的默认 base_url
    if channel and not provider.get('base_url'):
        if channel.default_base_url:
            provider = {**provider, 'base_url': channel.default_base_url}
    
    if channel and channel.request_adapter:
        # 先由具体渠道适配器构建 URL / headers / payload
        url, headers, payload = await channel.request_adapter(request, engine, provider, api_key)

        # 统一应用参数覆写（支持 all/*、模型别名、原始模型名，且深度合并）
        overrides = safe_get(provider, "preferences", "post_body_parameter_overrides", default={})
        model_dict = get_model_dict(provider)
        original_model = model_dict.get(request.model, request.model)

        if isinstance(overrides, dict) and overrides:
            def _deep_merge(target, override):
                if isinstance(target, dict) and isinstance(override, dict):
                    for _k, _v in override.items():
                        if isinstance(_v, dict) and isinstance(target.get(_k), dict):
                            _deep_merge(target[_k], _v)
                        else:
                            target[_k] = _v
                else:
                    return override

            # 全局 all / * 覆写
            for global_key in ("all", "*"):
                global_override = safe_get(overrides, global_key, default=None)
                if isinstance(global_override, dict):
                    _deep_merge(payload, global_override)

            # 模型别名和原始模型名覆写
            for model_key in {request.model, original_model}:
                model_override = safe_get(overrides, model_key, default=None)
                if isinstance(model_override, dict):
                    _deep_merge(payload, model_override)

            # 其余键（无空格和短横线）作为顶层字段覆写，保持与旧逻辑兼容
            for key, value in overrides.items():
                if key in ("all", "*", request.model, original_model):
                    continue
                if "-" not in key and " " not in key:
                    if key in payload and isinstance(payload[key], dict) and isinstance(value, dict):
                        _deep_merge(payload[key], value)
                    else:
                        payload[key] = value

        # 获取该渠道启用的插件列表
        enabled_plugins = safe_get(provider, "preferences", "enabled_plugins", default=None)

        from .log_config import logger
        logger.debug(f"[get_payload] Before apply_request_interceptors, model={payload.get('model')}, enabled_plugins={enabled_plugins}")

        # 应用请求拦截器（插件可在此修改 url/headers/payload）
        url, headers, payload = await apply_request_interceptors(
            request, engine, provider, api_key, url, headers, payload, enabled_plugins
        )

        logger.debug(f"[get_payload] After apply_request_interceptors, model={payload.get('model')}")

        return url, headers, payload
     
    raise ValueError(f"Unknown engine: {engine}")


async def prepare_request_payload(provider, request_data):
    """
    准备请求 payload 的便捷函数
    
    Args:
        provider: 提供商配置
        request_data: 请求数据字典
        
    Returns:
        tuple: (url, headers, payload, engine)
    """
    model_dict = get_model_dict(provider)
    request = RequestModel(**request_data)

    original_model = model_dict[request.model]
    engine, _ = get_engine(provider, endpoint=None, original_model=original_model)

    url, headers, payload = await get_payload(request, engine, provider, api_key=provider['api'])

    return url, headers, payload, engine
