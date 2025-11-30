"""
请求构建模块

负责根据不同引擎类型构建 API 请求的 URL、headers 和 payload
所有渠道通过 channels 模块的注册中心获取适配器
"""

from .models import RequestModel
from .utils import (
    get_engine,
    get_model_dict,
    safe_get,
)


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
     
     
    channel = get_channel(engine)
    if channel and channel.request_adapter:
        # 先由具体渠道适配器构建 URL / headers / payload
        url, headers, payload = await channel.request_adapter(request, engine, provider, api_key)

        # 统一应用 参数覆写
        # 1) all：对该渠道下所有模型生效
        # 2) <model_name>：仅对指定模型生效（按 RequestModel.model 别名匹配）
        overrides = safe_get(provider, "preferences", "post_body_parameter_overrides", default={})

        if isinstance(overrides, dict) and overrides:
            # 先应用全局 all 覆写：
            # - 对该渠道下所有模型生效
            # - 仅在 payload 中不存在同名字段时才写入，避免覆盖模型级覆写和适配器默认值
            all_overrides = safe_get(overrides, "all", default=None)
            if isinstance(all_overrides, dict):
                for k, v in all_overrides.items():
                    if k not in payload:
                        payload[k] = v

            # 再应用当前模型名对应的覆写：
            # - key 必须等于 request.model（即配置里的模型别名）
            # - 允许覆盖适配器默认写入以及 all 覆写
            model_overrides = safe_get(overrides, request.model, default=None)
            if isinstance(model_overrides, dict):
                for k, v in model_overrides.items():
                    payload[k] = v

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
