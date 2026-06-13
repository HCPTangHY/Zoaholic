"""
入站拦截器：剥离客户端 cache_control 字段

在请求进入 handler 后、分配给上游 provider 前，递归遍历 messages 和 system prompt，
移除所有 cache_control 字段。这样缓存策略完全由渠道 overrides 控制，
客户端自带的 ephemeral (5分钟) 等设置不会干扰。

启用方式：在渠道 preferences.enabled_plugins 中添加 "strip_cache_control"
"""

from core.plugins.interceptors import register_inbound_interceptor


def _strip_cache_control_from_value(obj):
    """递归剥离 dict/list 中所有 cache_control 字段"""
    if isinstance(obj, dict):
        obj.pop("cache_control", None)
        for v in obj.values():
            _strip_cache_control_from_value(v)
    elif isinstance(obj, list):
        for item in obj:
            _strip_cache_control_from_value(item)


async def strip_cache_control_interceptor(request_data, request, api_key_info, enabled_plugins):
    """
    剥离 request_data 中所有层级的 cache_control 字段。
    
    覆盖范围：
    - messages[].content (string 不处理，list of content blocks 处理)
    - messages[].content[].cache_control
    - system (string 不处理，list of content blocks 处理)
    - system[].cache_control
    - 顶层 cache_control
    """
    if request_data is None:
        return request_data
    
    # RequestModel 有 messages 属性
    messages = getattr(request_data, 'messages', None)
    if messages:
        for msg in messages:
            # msg 可能是 dict 或 pydantic model
            if isinstance(msg, dict):
                msg.pop("cache_control", None)
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        _strip_cache_control_from_value(block)
            else:
                # pydantic model — 通过 __dict__ 访问
                if hasattr(msg, '__dict__'):
                    msg.__dict__.pop("cache_control", None)
                content = getattr(msg, 'content', None)
                if isinstance(content, list):
                    for block in content:
                        _strip_cache_control_from_value(block)
    
    # 处理顶层 system prompt
    system = None
    if hasattr(request_data, 'system'):
        system = request_data.system
    elif isinstance(request_data, dict):
        system = request_data.get('system')
    
    if isinstance(system, list):
        for block in system:
            _strip_cache_control_from_value(block)
    
    # 处理 extra_body / 透传字段中的顶层 cache_control
    if isinstance(request_data, dict):
        request_data.pop("cache_control", None)
    elif hasattr(request_data, '__dict__'):
        request_data.__dict__.pop("cache_control", None)
    
    return request_data


# 注册为插件拦截器
# priority=50: 在其他可能的入站拦截器之前执行
register_inbound_interceptor(
    "strip_cache_control",
    strip_cache_control_interceptor,
    priority=50,
    plugin_name="strip_cache_control",
)
