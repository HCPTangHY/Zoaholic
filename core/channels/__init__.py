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
from . import openai_responses_channel
from . import gemini_channel
from . import claude_channel
from . import azure_channel
from . import aws_channel
from . import vertex_channel
from . import openrouter_channel
from . import cloudflare_channel
from . import openai_image_channel
# 修改原因：fal.ai 是新增的内置生成渠道，需要在 channels 包初始化时加载模块。
# 修改方式：与 openai_image_channel 一样显式导入 fal_channel。
# 目的：让注册表能够发现 fal 的 request/response/stream adapters。
from . import fal_channel
from . import codex_channel
from . import claude_code_channel
# 修改原因：Gemini CLI OAuth 是内置渠道，需要在 channels 包导入时进入注册流程。
# 修改方式：像 Codex 和 Claude Code 一样导入自包含渠道模块。
# 目的：让 core.channels.get_channel("gemini-cli") 能被请求路由和管理端发现。
from . import gemini_cli_channel
# 修改原因：Antigravity OAuth 是新的内置渠道，必须在 channels 包导入时加载模块定义。
# 修改方式：与其他自包含 OAuth 渠道一样显式导入 antigravity_channel。
# 目的：让注册表、OAuthManager 和管理端都能发现 antigravity engine。
from . import antigravity_channel

# 调用各渠道的 register() 函数
openai_channel.register()
openai_responses_channel.register()
gemini_channel.register()
claude_channel.register()
azure_channel.register()
aws_channel.register()
vertex_channel.register()
openrouter_channel.register()
cloudflare_channel.register()
openai_image_channel.register()
# 修改原因：仅导入 fal_channel 不会写入注册表，必须调用 register。
# 修改方式：在内置渠道注册序列中追加 fal_channel.register()。
# 目的：让 core.channels.get_channel("fal") 和管理端渠道列表都能使用 fal 渠道。
fal_channel.register()
codex_channel.register()
claude_code_channel.register()
# 修改原因：导入模块只加载定义，必须显式调用 register 才会写入渠道注册表。
# 修改方式：在 OAuth 内置渠道注册序列中追加 Gemini CLI。
# 目的：让 Gemini 方言透传和普通请求都可以选择 gemini-cli engine。
gemini_cli_channel.register()
# 修改原因：Antigravity 渠道同样自包含 provider 和 adapters，导入后还需要显式注册。
# 修改方式：在内置渠道注册序列末尾调用 antigravity_channel.register()。
# 目的：让 core.channels.get_channel("antigravity") 和 OAuth provider 扫描都能工作。
antigravity_channel.register()

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