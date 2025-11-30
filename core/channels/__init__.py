"""
渠道注册模块

自动导入并注册所有渠道适配器
支持通过 core.plugins 插件系统动态加载外部渠道
"""

from .registry import (
    ChannelDefinition,
    RequestAdapter,
    StreamAdapter,
    ResponseAdapter,
    register_channel,
    unregister_channel,
    get_channel,
    list_channels,
    list_channel_ids,
)

# 导入各渠道模块以触发注册
from . import openai_channel
from . import gemini_channel
from . import claude_channel
from . import azure_channel
from . import aws_channel
from . import vertex_channel
from . import openrouter_channel
from . import cloudflare_channel

# 调用各渠道的 register() 函数
openai_channel.register()
gemini_channel.register()
claude_channel.register()
azure_channel.register()
aws_channel.register()
vertex_channel.register()
openrouter_channel.register()
cloudflare_channel.register()

__all__ = [
    # 类型定义
    "ChannelDefinition",
    "RequestAdapter",
    "StreamAdapter",
    "ResponseAdapter",
    # 注册 API
    "register_channel",
    "unregister_channel",
    "get_channel",
    "list_channels",
    "list_channel_ids",
]