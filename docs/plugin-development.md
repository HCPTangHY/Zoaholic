# 插件开发指南

本文档介绍如何为 Zoaholic 开发和使用插件，并统一说明 UI 插槽、额度系统和 OAuth 渠道开发相关约定。本文合并自原 `plugin-development.md`、`ui-slots.md`、`quota-system.md` 和 `oauth-channels.md`。

## 目录

- [插件系统概述](#overview)
- [快速开始](#quick-start)
- [插件结构](#plugin-structure)
- [扩展点详解](#extension-points)
- [UI 插槽系统](#ui-slots)
- [额度系统](#quota-system)
- [OAuth 渠道开发](#oauth-channels)
- [附录/参考](#appendix-reference)

<a id="overview"></a>
## 插件系统概述

Zoaholic 的插件系统基于**扩展点（Extension Point）**机制，支持：

- **渠道适配器**（channels）：添加新的 API 渠道。
- **拦截器**（interceptors）：拦截和处理请求/响应。
- **中间件**（middlewares）：处理请求和响应。
- **处理器**（processors）：自定义数据处理。
- **格式转换器**（formatters）：格式转换。
- **验证器**（validators）：数据验证。

插件还可以配合以下系统扩展管理端和渠道能力：

- [UI 插槽系统](#ui-slots)：把少量前端展示逻辑声明到后端注册表，由管理端在固定位置挂载。
- [额度系统](#quota-system)：把普通 API Key 余额和 OAuth 账号额度归一化为统一结构。
- [OAuth 渠道开发](#oauth-channels)：通过 `OAuthProvider`、`OAuthManager` 和渠道注册表接入 OAuth 凭据、刷新和额度查询。

<a id="quick-start"></a>
## 快速开始

### 1. 创建插件文件

在 `plugins/` 目录下创建 Python 文件：

```python
# plugins/my_plugin.py

PLUGIN_INFO = {
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "我的第一个插件",
    "author": "Your Name",
}

def setup(manager):
    """插件初始化"""
    print(f"[{PLUGIN_INFO['name']}] 插件已加载!")

def teardown(manager):
    """插件清理"""
    print(f"[{PLUGIN_INFO['name']}] 插件已卸载!")
```

### 2. 加载插件

插件会在应用启动时自动加载，也可以手动加载：

```python
from core.plugins import get_plugin_manager

manager = get_plugin_manager()

# 加载所有插件
manager.load_all()

# 或加载单个插件
manager.load_plugin("plugins/my_plugin.py")
```

<a id="plugin-structure"></a>
## 插件结构

### `PLUGIN_INFO`（推荐）

定义插件元信息：

```python
PLUGIN_INFO = {
    "name": "plugin_name",           # 插件名称（必需）
    "version": "1.0.0",              # 版本号
    "description": "插件描述",        # 描述
    "author": "作者",                 # 作者
    "dependencies": ["other_plugin"], # 依赖的其他插件
    "metadata": {                     # 自定义元数据
        "category": "channel",
        "tags": ["example"],
    },
}
```

如果插件需要在管理端显示参数表单，应在 `PLUGIN_INFO["metadata"]` 中声明 UI 信息：

```python
PARAMS_SCHEMA = [
    {
        "key": "mode",
        "label": "处理模式",
        "type": "select",      # text / textarea / number / toggle / select / multi-select
        "default": "strip",
        "options": [
            {"value": "allow", "label": "允许"},
            {"value": "strip", "label": "剥离"},
        ],
        "serialize": "key_value",
    },
    {
        "key": "keywords",
        "label": "关键词",
        "type": "textarea",
        "placeholder": "一行一个关键词",
        "serialize": "key_value",
    },
]

PLUGIN_INFO = {
    "name": "my_plugin",
    "version": "1.0.0",
    "description": "示例插件",
    "metadata": {
        "category": "interceptors",
        "params_hint": "mode=strip,keywords=...",
        "params_schema": PARAMS_SCHEMA,
    },
}
```

UI 声明规则：

- `params_hint` 是简短说明，显示给管理员查看。
- `params_schema` 是前端参数表单的字段定义。
- `serialize: "key_value"` 表示保存为 `key=value` 参数。多个参数会由前端按插件条目格式写入 `enabled_plugins`。
- 如果同一插件注册了拦截器，建议把 `params_hint` 和 `params_schema` 同时写入拦截器的 `metadata`。这样 `/v1/plugins/interceptors` 可以直接把参数表单带给渠道面板和 Key 面板。

### `EXTENSIONS`（可选）

声明插件提供的扩展：

```python
EXTENSIONS = [
    "channels:my_channel",       # 渠道扩展
    "middlewares:my_middleware", # 中间件扩展
]
```

### 生命周期函数

```python
def setup(manager):
    """
    插件初始化（推荐）

    当插件被加载并激活时调用。
    在这里注册扩展到插件系统。

    Args:
        manager: PluginManager 实例
    """
    pass

def teardown(manager):
    """
    插件清理（可选）

    当插件被卸载时调用。
    在这里清理资源和注销扩展。

    Args:
        manager: PluginManager 实例
    """
    pass

def unload():
    """
    插件卸载回调（可选）

    当插件模块被从内存中移除前调用。
    用于清理全局状态或关闭连接等。
    """
    pass
```

<a id="extension-points"></a>
## 扩展点详解

### 渠道扩展（channels）

用于添加新的 API 渠道适配器：

```python
def setup(manager):
    manager.register_extension(
        extension_point="channels",
        extension_id="my_channel",
        implementation=MyChannelAdapter,
        priority=100,
        metadata={"description": "我的渠道"},
        plugin_name=PLUGIN_INFO["name"],
    )
```

渠道适配器需要实现以下接口：

```python
class MyChannelAdapter:
    id = "my_channel"
    type_name = "my_provider"

    @staticmethod
    async def request_adapter(request, engine, provider, api_key=None):
        """构建请求"""
        url = provider.get('base_url')
        headers = {'Authorization': f'Bearer {api_key}'}
        payload = {...}
        return url, headers, payload

    @staticmethod
    async def stream_adapter(client, url, headers, payload, engine, model, timeout):
        """处理流式响应"""
        async with client.stream('POST', url, ...) as response:
            async for line in response.aiter_lines():
                yield f"data: {line}\n\n"
        yield "data: [DONE]\n\n"

    @staticmethod
    async def response_adapter(client, url, headers, payload, engine, model, timeout):
        """处理非流式响应"""
        response = await client.post(url, ...)
        yield f"data: {response.json()}\n\n"
```

同时注册到渠道注册表，以保持兼容性：

```python
from core.channels.registry import register_channel

register_channel(
    id="my_channel",
    type_name="my_provider",
    request_adapter=MyChannelAdapter.request_adapter,
    stream_adapter=MyChannelAdapter.stream_adapter,
    response_adapter=MyChannelAdapter.response_adapter,
)
```

如果渠道需要管理端展示能力，可以在 `register_channel()` 中同时注册 `ui_slots`。详见 [UI 插槽系统](#ui-slots)。如果渠道使用 OAuth 凭据，请参考 [OAuth 渠道开发](#oauth-channels)。

### 请求/响应拦截器（推荐）

插件可以通过拦截器修改请求和响应。当前拦截器分为以下阶段：

- **Key 入站**：`register_inbound_interceptor`。鉴权完成后、选择 provider 前执行。配置来自 `api_keys[].preferences.enabled_plugins`。
- **渠道入站**：`register_channel_inbound_interceptor`。provider 已选定、channel adapter 转换请求体前执行。配置来自 `provider.preferences.enabled_plugins`。
- **请求拦截**：`register_request_interceptor`。请求体已转换为上游协议、发送到上游前执行。配置来自 `provider.preferences.enabled_plugins`。
- **响应拦截**：`register_response_interceptor`。上游返回后执行。配置来自 `provider.preferences.enabled_plugins`。
- **渠道出站**：`register_channel_outbound_interceptor`。响应拦截器和 Key Rules 后、Key 出站前执行。配置来自 `provider.preferences.enabled_plugins`。
- **Key 出站**：`register_key_outbound_interceptor`。渠道出站之后、最终返回客户端前执行。配置来自 `api_keys[].preferences.enabled_plugins`。

请求拦截和响应拦截是最常用的渠道级阶段：

```python
from core.plugins import (
    register_request_interceptor,
    unregister_request_interceptor,
    register_response_interceptor,
    unregister_response_interceptor,
)

# 请求拦截器：在请求发送到渠道前调用
async def my_request_interceptor(request, engine, provider, api_key, url, headers, payload):
    """
    拦截并修改请求参数

    Args:
        request: 原始请求对象
        engine: 引擎类型 (openai, gemini, claude, etc.)
        provider: 提供商配置
        api_key: API 密钥
        url: 请求 URL
        headers: 请求头
        payload: 请求体

    Returns:
        (url, headers, payload) 修改后的请求参数
    """
    # 添加自定义 header
    headers["X-Custom-Header"] = "value"

    # 修改 payload
    payload["custom_param"] = "value"

    return url, headers, payload

# 响应拦截器：在响应返回时调用
async def my_response_interceptor(response_chunk, engine, model, is_stream):
    """
    拦截并处理响应数据

    Args:
        response_chunk: 响应数据（流式时为单个 chunk，非流式时为完整响应）
        engine: 引擎类型
        model: 模型名称
        is_stream: 是否为流式响应

    Returns:
        修改后的响应数据
    """
    # 可以记录日志、修改响应等
    return response_chunk

def setup(manager):
    # 注册拦截器
    register_request_interceptor(
        interceptor_id="my_request_interceptor",
        callback=my_request_interceptor,
        priority=100,  # 数值越小越先执行
        plugin_name="my_plugin",
    )

    register_response_interceptor(
        interceptor_id="my_response_interceptor",
        callback=my_response_interceptor,
        priority=100,
        plugin_name="my_plugin",
    )

def teardown(manager):
    # 注销拦截器
    unregister_request_interceptor("my_request_interceptor")
    unregister_response_interceptor("my_response_interceptor")
```

#### 拦截器执行顺序

一次请求中的主要顺序如下：

1. **Key 入站**：下游 API Key 鉴权完成后执行，可检查请求来源、系统提示词、工具声明等。
2. **渠道入站**：provider 选定后执行，可做渠道级请求对象清洗。
3. **请求拦截**：channel adapter 生成上游请求后执行，可改 URL、headers、payload。
4. **上游请求**：向真实 provider 发送请求。
5. **响应拦截**：上游响应返回后执行，可改响应 chunk 或完整响应。
6. **Key Rules**：按渠道配置处理错误、冷却和重试。
7. **渠道出站**：渠道级最终响应处理。
8. **Key 出站**：下游 API Key 级最终响应处理。
9. **返回客户端**。

每个阶段内部按 `priority` 排序执行，数值越小越先执行。

#### 按渠道控制拦截器

拦截器支持按渠道启用或禁用。在渠道配置的 `preferences.enabled_plugins` 中指定要启用的插件列表：

```yaml
providers:
  - provider: my_provider
    base_url: https://api.example.com
    preferences:
      enabled_plugins:
        - claude_thinking
        - my_custom_plugin
```

只有在 `enabled_plugins` 列表中的插件的拦截器才会被执行。如果未配置 `enabled_plugins`，则所有启用的拦截器都会执行。

在前端渠道编辑界面中，可以通过“插件拦截器”部分配置要启用的插件。

#### 全局拦截器（无条件执行）

注册拦截器时不设置 `plugin_name` 参数，则该拦截器为**全局拦截器**，对所有请求无条件执行，不受渠道的 `enabled_plugins` 控制。

```python
# 插件拦截器：受 enabled_plugins 控制
register_request_interceptor(
    interceptor_id="my_request_interceptor",
    callback=my_request_interceptor,
    priority=100,
    plugin_name="my_plugin",  # 有 plugin_name → 需要渠道启用才执行
)

# 全局拦截器：无条件执行
register_request_interceptor(
    interceptor_id="my_global_interceptor",
    callback=my_global_interceptor,
    priority=3,
    # 不设置 plugin_name → 所有请求都会经过
)
```

适用场景：渠道内置的全局逻辑，例如 AWS 的 SigV4 签名、Bedrock `cache_control` 清除等。通常在渠道的 `register()` 函数中注册，并在回调内通过 `engine` 参数判断是否为目标引擎。

#### Key 入站、渠道出站和 Key 出站示例

Key 入站适合做下游 API Key 级别的访问控制。`key_guard` 就使用这个阶段检查 User-Agent、请求头、系统提示词和工具声明：

```python
from core.plugins import register_inbound_interceptor, unregister_inbound_interceptor

PARAMS_SCHEMA = [
    {"key": "allowed_ua", "label": "UA 白名单", "type": "textarea", "serialize": "key_value"},
]

async def my_key_inbound(request_data, request, api_key_info, enabled_plugins):
    # enabled_plugins 来自 api_keys[].preferences.enabled_plugins
    # api_key_info 中包含 api_key、api_index、model 等鉴权结果
    return request_data

register_inbound_interceptor(
    "my_key_inbound",
    my_key_inbound,
    priority=30,
    plugin_name="my_plugin",
    metadata={
        "description": "Key 入站检查示例",
        "params_hint": "allowed_ua=TauriTavern",
        "params_schema": PARAMS_SCHEMA,
    },
)
```

渠道出站适合做 provider 级别的最终响应处理。它和渠道配置绑定：

```python
from core.plugins import register_channel_outbound_interceptor, unregister_channel_outbound_interceptor

async def my_channel_outbound(response_chunk, engine, model, provider, is_stream, enabled_plugins):
    # enabled_plugins 来自 provider.preferences.enabled_plugins
    # provider 是当前选中的上游渠道配置
    return response_chunk

register_channel_outbound_interceptor(
    "my_channel_outbound",
    my_channel_outbound,
    priority=100,
    plugin_name="my_plugin",
    metadata={"description": "渠道出站处理示例"},
)
```

Key 出站适合做下游 API Key 级别的最终响应处理。它和 Key 配置绑定：

```python
from core.plugins import register_key_outbound_interceptor, unregister_key_outbound_interceptor

async def my_key_outbound(response_chunk, engine, model, api_key_info, is_stream, enabled_plugins):
    # enabled_plugins 来自 api_keys[].preferences.enabled_plugins
    # api_key_info 是当前下游 API Key 的信息
    return response_chunk

register_key_outbound_interceptor(
    "my_key_outbound",
    my_key_outbound,
    priority=100,
    plugin_name="my_plugin",
    metadata={"description": "Key 出站处理示例"},
)
```

卸载插件时需要注销已注册的拦截器：

```python
def teardown(manager):
    unregister_inbound_interceptor("my_key_inbound")
    unregister_channel_outbound_interceptor("my_channel_outbound")
    unregister_key_outbound_interceptor("my_key_outbound")
```

#### 前端识别拦截器阶段

前端通过 `/v1/plugins/interceptors` 读取插件能力。接口会返回以下阶段字段：

- `inbound_interceptors`：Key 入站。
- `channel_inbound_interceptors`：渠道入站。
- `request_interceptors`：请求拦截。
- `response_interceptors`：响应拦截。
- `channel_outbound_interceptors`：渠道出站。
- `key_outbound_interceptors`：Key 出站。

注册拦截器时，系统会自动在条目 `metadata.stage` 中写入对应阶段。插件也可以显式写入同名 `stage`，但一般不需要手动写。渠道面板和 Key 面板会根据这些字段决定插件显示在哪个位置。

插件参数表单来自 `metadata.params_schema`。如果插件只在 `PLUGIN_INFO["metadata"]` 中声明，插件列表接口也会返回该声明。为了兼容旧前端，建议在每个拦截器注册时也把 `params_schema` 放入拦截器 `metadata`。

#### 拦截器管理 API

```python
from core.plugins import get_interceptor_registry

registry = get_interceptor_registry()

# 获取所有拦截器
request_interceptors = registry.get_request_interceptors()
response_interceptors = registry.get_response_interceptors()

# 启用/禁用拦截器
registry.enable_request_interceptor("my_interceptor")
registry.disable_request_interceptor("my_interceptor")

# 按插件注销所有拦截器
registry.unregister_plugin_interceptors("my_plugin")

# 获取统计信息
stats = registry.get_stats()

# 获取所有注册了拦截器的插件列表
interceptor_plugins = registry.get_interceptor_plugins()
```

### 中间件扩展（middlewares）

用于处理请求和响应：

```python
class MyMiddleware:
    async def process_request(self, request):
        """处理请求"""
        # 修改或验证请求
        return request

    async def process_response(self, response):
        """处理响应"""
        # 修改或处理响应
        return response

    async def on_error(self, error):
        """错误处理"""
        pass

def setup(manager):
    manager.register_extension(
        extension_point="middlewares",
        extension_id="my_middleware",
        implementation=MyMiddleware(),
        priority=50,  # 优先级越小越先执行
    )
```

### 生命周期钩子扩展（hooks）

用于监听应用生命周期事件。它不同于请求/响应拦截器：

```python
class MyLifecycleHooks:
    async def on_startup(self):
        """应用启动时"""
        pass

    async def on_shutdown(self):
        """应用关闭时"""
        pass

    async def before_request(self, request):
        """请求处理前"""
        pass

    async def after_request(self, request, response):
        """请求处理后"""
        pass

    async def on_error(self, error):
        """发生错误时"""
        pass

def setup(manager):
    manager.register_extension(
        extension_point="hooks",
        extension_id="my_lifecycle_hooks",
        implementation=MyLifecycleHooks(),
    )
```

<a id="ui-slots"></a>
## UI 插槽系统

UI 插槽系统用于让渠道和插件把少量前端展示逻辑声明到后端注册表，由管理端在固定位置挂载，而不是在 `Channels.tsx` 中继续硬编码每个渠道。插槽常用于额度展示、渠道提示、导入占位和背景样式。

### 插槽注册表

插槽注册表位于 `core/ui_slots/registry.py`。

#### `register_ui_slot`

`register_ui_slot` 用于注册一个全局 UI 插槽贡献。

```python
register_ui_slot(
    slot_id="plugin_name.quota_display",
    slot="quota_display",
    script="export default function render(ctx) { ctx.el.textContent = 'ok'; }",
    source="plugin_name",
    priority=100,
    mode="replace",
    engines=["openai"],
    auth_types=["api_key"],
    enabled_plugin="plugin_name",
)
```

参数说明：

| 参数 | 类型 | 说明 |
|---|---|---|
| `slot_id` | `str` | 全局唯一 ID。重复注册同一 ID 会覆盖旧贡献。 |
| `slot` | `str` | 插槽名，例如 `quota_display`、`key_background`。 |
| `script` | `str` | 内联 JavaScript 脚本。 |
| `source` | `str` | 来源渠道或插件名。 |
| `priority` | `int` | 优先级。多个贡献命中同一插槽时，数值大的生效。 |
| `mode` | `str` | 当前支持 `replace` 和 `append`。现阶段主要使用 `replace`。 |
| `engines` | `Optional[List[str]]` | 限定 engine。`None` 表示所有 engine。 |
| `auth_types` | `Optional[List[str]]` | 限定认证类型，例如 `api_key`、`oauth`。 |
| `enabled_plugin` | `Optional[str]` | 要求 provider 启用指定插件后才生效。 |

#### `unregister_ui_slot`

`unregister_ui_slot(slot_id)` 用于注销一个贡献。插件卸载或热重载时必须调用它，避免旧脚本继续留在内存中。

#### `resolve_slots_for_engine`

`resolve_slots_for_engine(engine, auth_type="api_key", enabled_plugins=None, channel_slots=None)` 用于解析某个 engine 最终应该输出的插槽脚本。

解析顺序：

1. 先复制渠道自身的 `channel_slots`。
2. 遍历全局贡献。
3. 按 `engines`、`auth_types`、`enabled_plugin` 过滤。
4. 同一插槽只选择 `priority` 最大的贡献。
5. `replace` 会覆盖基础 slot；`append` 只在基础 slot 不存在时写入。

### `UiSlotContribution` 数据模型

`UiSlotContribution` 是注册表内部保存的贡献模型。

| 字段 | 类型 | 说明 |
|---|---|---|
| `slot_id` | `str` | 全局唯一贡献 ID。 |
| `slot` | `str` | 插槽名。 |
| `script` | `str` | 内联 JS 脚本。 |
| `source` | `str` | 来源渠道或插件。 |
| `priority` | `int` | 优先级，越大越优先。 |
| `mode` | `str` | 合并模式。 |
| `engines` | `Optional[List[str]]` | engine 匹配条件。 |
| `auth_types` | `Optional[List[str]]` | 认证类型匹配条件。 |
| `enabled_plugin` | `Optional[str]` | provider 插件开关匹配条件。 |

### target 匹配条件

#### `engines`

`engines` 限定某个贡献只对指定渠道生效。

示例：

```python
engines=["codex", "openai-responses"]
```

如果为 `None` 或空值，则不限制 engine。

#### `auth_types`

`auth_types` 限定认证类型。

当前约定值：

- `api_key`：普通 API Key 渠道。
- `oauth`：OAuth 凭据渠道。

`ChannelDefinition.to_dict()` 会根据 `channel.is_oauth` 自动传入 `oauth` 或 `api_key`。

#### `enabled_plugin`

`enabled_plugin` 限定 provider 配置中启用了某个插件时才生效。该条件适合 provider 级解析。`/v1/channels` 阶段没有具体 provider 上下文，因此依赖 `enabled_plugin` 的贡献通常不会在全局渠道列表中输出。

### 可用插槽位

| 插槽名 | 位置 | 功能 |
|---|---|---|
| `quota_display` | 机房卡片圆环中心 / 完整行右侧 | 额度展示 |
| `key_border` | 卡片/行覆盖层 | 额度边框效果 |
| `key_background` | 卡片/行背景层 | 渠道专属背景 |
| `key_hint` | 配置页 key 输入框下方 | key 格式提示 |
| `base_url_hint` | 配置页 base_url 下方 | URL 格式提示 |
| `token_url_hint` | 配置页 token_url 下方 | token URL 提示 |
| `balance_summary` | 配置页余额区域 | 替代默认余额表单 |
| `import_placeholder` | 导入弹窗输入框 | 占位提示文本（纯文本，非 JS） |
| `override_hint` | 渠道配置区域 | 覆写提示 |

### 渠道注册插槽

渠道可以在 `register_channel` 的 `ui_slots` 参数中注册插槽。该方式适合渠道内置 UI，例如 OAuth 渠道的导入占位、额度标签、服务账号提示。

```python
from core.channels.registry import register_channel

QUOTA_DISPLAY = """
export default function render(ctx) {
    const { el, data } = ctx || {};
    if (!el) return;
    const percent = typeof data?.percent === 'number' ? Math.round(data.percent) : null;
    el.textContent = percent == null ? '' : percent + '%';
}
""".strip()

register_channel(
    id="example-oauth",
    type_name="openai",
    is_oauth=True,
    oauth_provider=ExampleProvider(),
    ui_slots={
        "quota_display": QUOTA_DISPLAY,
        "import_placeholder": "rt_xxxxxxxx...",
    },
)
```

注意事项：

1. `import_placeholder` 是纯文本，不是 JavaScript。
2. 其他插槽通常是内联 JS 模块。
3. 渠道自带 `ui_slots` 会作为基础 slot，再与全局注册表贡献合并。
4. 如果渠道没有实际额度数据，不应为了占位强行注册 `quota_display`。

### 插件注册插槽

插件可以在 `setup()` 中调用 `register_ui_slot()`，并在 `teardown()` 中调用 `unregister_ui_slot()`。

```python
PLUGIN_INFO = {"name": "example_quota_badge"}

SCRIPT = """
export default function render(ctx) {
    const { el, data } = ctx || {};
    if (!el || !data?.tier) return;
    el.textContent = data.tier;
}
""".strip()


def setup(manager):
    from core.ui_slots.registry import register_ui_slot

    register_ui_slot(
        slot_id="example_quota_badge.quota_display",
        slot="quota_display",
        script=SCRIPT,
        source=PLUGIN_INFO["name"],
        priority=50,
        engines=["openai"],
        auth_types=["api_key"],
        enabled_plugin="example_quota_badge",
    )


def teardown(manager):
    from core.ui_slots.registry import unregister_ui_slot

    unregister_ui_slot("example_quota_badge.quota_display")
```

插件注册规则：

1. `slot_id` 必须稳定且全局唯一。
2. `teardown()` 必须注销已注册贡献。
3. 如果脚本依赖 provider 的插件开关，应设置 `enabled_plugin`。
4. 如果脚本只适用于普通 Key 或 OAuth，应设置 `auth_types`。

### 内联 JS 脚本规范

除 `import_placeholder` 外，插槽脚本应使用 ES module 默认导出函数：

```js
export default function render(ctx) {
    const { el, data, context } = ctx;
}
```

完整上下文形态为 `{el, data, context}`。

#### `ctx`

`ctx` 是前端 `UiSlot` 组件传入的对象。

| 字段 | 说明 |
|---|---|
| `el` | DOM 元素。脚本只能在这个元素内写内容或样式。 |
| `data` | 额度数据。通常是 `BalanceResult` 或 OAuthQuota。 |
| `context` | 附加上下文。当前约定为 `{account, keyObj, balance}`。 |

#### `el`

`el` 是当前插槽的 DOM 元素。脚本应只操作 `el` 或它自己创建的子节点。

建议：

1. 写纯文本时使用 `el.textContent`。
2. 需要复杂结构时先清空 `el.innerHTML`，再创建子元素。
3. 注册 `ResizeObserver` 或事件监听时，应把清理函数保存到 `el` 上，并在下次 render 前执行。

#### `data`

`data` 是额度数据，可能来自：

- 普通 `BalanceResult`。
- OAuthQuota 兼容对象。
- `buildRowQuotaSlotData()` 从 `gauges` 和账号状态构造的兼容数据。

常见字段：

- `percent`
- `quota_inner`
- `quota_outer`
- `raw`
- `gauges`
- `badges`

脚本应先判断字段是否存在，不应假设所有渠道都有同样结构。额度数据模型详见 [额度系统](#quota-system)。

#### `context`

`context` 当前包含 `{account, keyObj, balance}`。

| 字段 | 说明 |
|---|---|
| `account` | OAuth 账号状态。普通 Key 可能为空。 |
| `keyObj` | 当前 Key 行对象，包含 key、label、disabled 等。 |
| `balance` | 当前 Key 的 BalanceResult。 |

示例：

```js
export default function render(ctx) {
    const { el, data, context } = ctx || {};
    if (!el) return;

    const account = context?.account;
    const inner = typeof data?.quota_inner === 'number' ? data.quota_inner : null;
    const outer = typeof data?.quota_outer === 'number' ? data.quota_outer : null;
    const values = [inner, outer].filter(v => v != null);
    const percent = values.length ? Math.round(Math.min(...values)) : null;

    if (percent == null && !account?.status) {
        el.textContent = '';
        el.style.display = 'none';
        return;
    }

    el.style.display = '';
    el.textContent = percent == null ? account.status : percent + '%';
}
```

### 安全和维护要求

1. `import_placeholder` 必须是纯文本，不能写 JS。
2. 插槽脚本不要访问全局敏感变量。
3. 插槽脚本不要发起网络请求。
4. 插槽脚本应能处理 `data`、`context` 或其子字段为空的情况。
5. 插槽脚本中涉及渠道私有字段时，应在渠道文档或注释中说明来源。

<a id="quota-system"></a>
## 额度系统

统一额度系统用于让普通 API Key 余额和 OAuth 账号额度使用同一套结构，同时保留旧字段，避免破坏旧前端和插件。额度数据常通过 [UI 插槽系统](#ui-slots) 的 `quota_display`、`key_border`、`balance_summary` 等插槽展示。

### 数据模型

数据模型位于 `core/quota/types.py`。它们使用 Python `dataclass` 定义，最终通过 `QuotaSnapshot.to_dict()` 输出到 API 响应。

#### `QuotaSnapshot`

`QuotaSnapshot` 是统一额度快照。普通 Key 的 `BalanceResult` 和 OAuth 账号额度都会先转成这个结构，再输出给前端。

| 字段 | 类型 | 用途 |
|---|---|---|
| `supported` | `bool` | 表示当前渠道是否支持额度查询。 |
| `status` | `str` | 查询状态。当前约定值包括 `ok`、`error`、`unknown`、`unsupported`。 |
| `error` | `Optional[str]` | 查询失败时的错误信息。 |
| `value_type` | `str` | 兼容旧余额类型。常见值为 `amount`、`percent`、`quota`。 |
| `total` | `Optional[float]` | 总额度。普通余额常用。 |
| `used` | `Optional[float]` | 已使用额度。普通余额常用。 |
| `available` | `Optional[float]` | 可用额度。普通余额或 OAuth 兼容百分比会使用。 |
| `percent` | `Optional[float]` | 可用百分比，范围按 0 到 100 处理。 |
| `currency` | `Optional[str]` | 金额或额度单位，例如 `credits`。 |
| `expires_at` | `Optional[str]` | 额度过期时间或重置时间。 |
| `gauges` | `list[QuotaGauge]` | 圆环、弧线或进度条数据。前端主要展示入口。 |
| `badges` | `list[QuotaBadge]` | 标签药丸数据，例如套餐、Tier、订阅类型。 |
| `metrics` | `dict` | 通用指标扩展，例如 `rpm`、`tpm`。当前默认输出为空字典。 |
| `extensions` | `dict` | 渠道或插件私有扩展数据。当前默认输出为空字典。 |
| `raw` | `Any` | 上游原始数据或缓存原始数据。 |
| `tier` | `Optional[str]` | 旧字段，保留给 OpenAI Tier 等旧插件和旧 UI。 |
| `quota_inner` | `Optional[float]` | 旧字段，通常表示 OAuth 短窗口额度。 |
| `quota_outer` | `Optional[float]` | 旧字段，通常表示 OAuth 长窗口额度。 |

#### `QuotaGauge`

`QuotaGauge` 表示一个圆环或进度条。前端的 `QuotaRings` 会根据 gauge 数量自动选择空环、单环或双环展示。

| 字段 | 类型 | 用途 |
|---|---|---|
| `id` | `str` | gauge 唯一标识，例如 `balance`、`inner`、`outer`、`5h`、`7d`。 |
| `label` | `str` | 展示名或提示名，例如 `余额`、`inner`、`outer`。 |
| `role` | `str` | 语义角色。常见值为 `primary`、`secondary`、`short_window`、`long_window`。 |
| `percent` | `Optional[float]` | 可用百分比，范围应为 0 到 100。 |
| `total` | `Optional[float]` | 总额度。 |
| `available` | `Optional[float]` | 可用额度。 |
| `used` | `Optional[float]` | 已用额度。 |
| `tone` | `Optional[str]` | 建议色调。前端识别 `blue`、`green`、`yellow`、`red`、`gray`。 |
| `resets_at` | `Optional[str]` | 重置时间，建议使用 ISO datetime。 |
| `unit` | `Optional[str]` | 单位，例如 `credits`、`tokens`、`requests`。 |

#### `QuotaBadge`

`QuotaBadge` 表示一个标签药丸。

| 字段 | 类型 | 用途 |
|---|---|---|
| `id` | `str` | badge 唯一标识，例如 `tier`、`plan_type`、`subscription`。 |
| `label` | `str` | 展示文本，例如 `Tier 3`、`Pro`、`Plus`。 |
| `tone` | `str` | 标签色调。前端识别 `blue`、`green`、`yellow`、`red`、`gray`。默认是 `blue`。 |
| `priority` | `int` | 展示优先级，数值越大越靠前。默认是 `100`。 |
| `source` | `str` | 来源渠道或插件名，例如 `balance`、`oai_tier`。 |

#### 旧字段兼容策略

新系统只追加新结构，不删除旧字段。`QuotaSnapshot.to_dict()` 会继续输出旧字段和旧语义：

- `total`
- `used`
- `available`
- `percent`
- `tier`
- `quota_inner`
- `quota_outer`

兼容策略如下：

1. 普通余额仍以 `total`、`used`、`available`、`percent` 表达主额度。
2. OAuth 双窗口额度仍保留 `quota_inner` 和 `quota_outer`。
3. `tier` 仍保留在顶层，同时会被转换成 `badges` 中的 `tier` 标签。
4. 新字段 `gauges`、`badges`、`metrics`、`extensions` 始终按稳定结构输出；即使为空，也输出空数组或空对象。
5. 前端优先读取 `gauges` 和 `badges`；缺失时再回退到旧字段。

### API

#### `/v1/channels/balance`

`/v1/channels/balance` 是普通渠道和 OAuth 渠道共同使用的余额入口。

普通渠道路径：

1. 路由读取 provider 配置。
2. 调用 `core.balance.query_provider_balance()`。
3. 通过 `from_balance_result()` 追加统一字段。
4. 返回旧字段和新字段的合并结果。

OAuth 渠道路径：

1. 路由通过渠道注册表判断 `channel.is_oauth=True`。
2. 调用 `OAuthManager.fetch_quota(channel_id, key_id, force=True)`。
3. 把 OAuth quota 转为旧 `BalanceResult` 形状。
4. 通过 `from_oauth_account()` 和 `from_balance_result()` 追加统一字段。
5. 返回旧字段、新字段和逐账号 `results`。

#### 响应新增字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `gauges` | `array` | 圆环或进度条列表。 |
| `badges` | `array` | 标签药丸列表。 |
| `metrics` | `object` | 结构化指标扩展。 |
| `extensions` | `object` | 渠道或插件私有扩展。 |

这些字段不会替代旧字段。调用方可以继续读取 `total`、`used`、`available`、`percent`、`tier`、`quota_inner`、`quota_outer`。

#### 示例响应 JSON

普通余额示例：

```json
{
  "supported": true,
  "status": "ok",
  "value_type": "amount",
  "total": 100.0,
  "used": 25.0,
  "available": 75.0,
  "percent": 75.0,
  "currency": "credits",
  "tier": "Tier 3",
  "raw": {
    "source": "balance-api"
  },
  "gauges": [
    {
      "id": "balance",
      "label": "余额",
      "role": "primary",
      "percent": 75.0,
      "total": 100.0,
      "available": 75.0,
      "used": 25.0,
      "unit": "credits"
    }
  ],
  "badges": [
    {
      "id": "tier",
      "label": "Tier 3",
      "tone": "blue",
      "priority": 100,
      "source": "balance"
    }
  ],
  "metrics": {},
  "extensions": {}
}
```

OAuth 额度示例：

```json
{
  "supported": true,
  "status": "ok",
  "value_type": "percent",
  "total": 100.0,
  "used": 40.0,
  "available": 60.0,
  "percent": 60.0,
  "quota_inner": 80.0,
  "quota_outer": 60.0,
  "raw": {
    "reset_requests": "10m"
  },
  "gauges": [
    {
      "id": "inner",
      "label": "inner",
      "role": "short_window",
      "percent": 80.0
    },
    {
      "id": "outer",
      "label": "outer",
      "role": "long_window",
      "percent": 60.0
    }
  ],
  "badges": [],
  "metrics": {},
  "extensions": {},
  "results": {
    "user@example.com": {
      "supported": true,
      "value_type": "percent",
      "total": 100.0,
      "used": 40.0,
      "available": 60.0,
      "percent": 60.0,
      "quota_inner": 80.0,
      "quota_outer": 60.0,
      "raw": {
        "reset_requests": "10m"
      },
      "error": null
    }
  }
}
```

### Normalizers

归一化函数位于 `core/quota/normalizers.py`。

#### `from_balance_result(bal: dict)`

`from_balance_result(bal: dict)` 把普通 Key 余额结果转换为 `QuotaSnapshot`。

主要行为：

1. 复制旧字段：`supported`、`value_type`、`total`、`used`、`available`、`percent`、`currency`、`expires_at`、`raw`、`tier`、`quota_inner`、`quota_outer`。
2. 如果 `percent` 或 `available` 存在，生成一个 `id="balance"` 的主 `QuotaGauge`。
3. 如果 `percent` 缺失，但 `total > 0` 且 `available` 存在，用 `available / total * 100` 补算 gauge 百分比。
4. 如果存在 `tier`，生成一个来源为 `balance` 的蓝色 `QuotaBadge`。

适用场景：普通 Key 余额查询、插件补充 tier 后的余额结果、部分返回单一百分比的渠道。

#### `from_oauth_account(account: dict)`

`from_oauth_account(account: dict)` 把 OAuth 账号额度转换为 `QuotaSnapshot`。

主要行为：

1. 读取 `quota_raw` 或 `raw` 写入 `raw`。
2. 读取 `quota_inner` 写入短窗口 gauge：`id="inner"`、`role="short_window"`。
3. 读取 `quota_outer` 写入长窗口 gauge：`id="outer"`、`role="long_window"`。
4. 从所有 gauge 的 `percent` 中取最小值作为兼容 `percent`。
5. 保留顶层 `quota_inner` 和 `quota_outer`，供旧前端、默认边框和旧插槽脚本继续使用。

适用场景：Codex、Claude Code、Antigravity 等可以返回 OAuth quota 的渠道。

### 前端组件

统一额度前端入口位于 `frontend/src/pages/Channels.tsx`。

#### `QuotaRings`

`QuotaRings` 是通用圆环组件。它只接收 `gauges`，不关心渠道类型。

展示规则：

1. `0` 个 gauge：显示空环和 `—`。
2. `1` 个 gauge：显示单环，圆心显示百分比或 `displayLabel`。
3. `2` 个 gauge：显示双环，第一项为外层环，第二项为内层环。
4. 超过 `2` 个 gauge：当前只取前两个展示。
5. 如果 gauge 提供 `tone`，使用通用色调映射；否则按余额百分比分档或默认蓝紫色处理。

#### `QuotaBadges`

`QuotaBadges` 是通用标签组件。它只接收 `badges`，不读取渠道私有字段。

展示规则：

1. 过滤空 `label`。
2. 按 `priority` 降序排序。
3. 根据 `tone` 映射药丸样式。
4. 如果 `source` 存在，写入 `title`，便于调试来源。

#### `buildRowQuota`

`buildRowQuota` 是统一行模型构建函数。机房卡片和完整 Key 行都通过它得到同一份 `RowQuota`。

输入：

- `bal: BalanceResult | undefined`
- `oauthAccount: any`
- `isOAuthEngine: boolean`

输出：

```ts
interface RowQuota {
  gauges: QuotaGauge[];
  badges: QuotaBadge[];
}
```

构建顺序：

1. 如果 `bal.gauges` 存在且非空，优先使用后端新字段，同时读取 `bal.badges`。
2. OAuth 渠道缺少 `gauges` 时，从账号状态或旧 `BalanceResult` 的 `quota_inner`、`quota_outer` 构造双 gauge。
3. 普通渠道缺少 `gauges` 时，从 `percent` 或 `available / total` 构造主 gauge。
4. 如果 `bal.badges` 存在，使用它；否则如果 `bal.tier` 存在，构造 `tier` badge。

相关辅助函数：

- `buildRowQuotaSlotData()`：为旧 `quota_display`、`key_background` 等插槽构造兼容 `data`。
- `getQuotaPairFromGauges()`：从前两个 gauge 提取 `quota_inner`、`quota_outer`，供默认边框使用。
- `withRackCompactBalanceFallback()`：机房卡片对旧 amount 余额使用紧凑显示文本。

<a id="oauth-channels"></a>
## OAuth 渠道开发

OAuth 渠道开发涉及后端凭据状态、token 刷新、额度缓存、渠道注册、UI 插槽和账号复制改名行为。本文只描述当前代码路径和约定，不代表所有渠道都支持浏览器登录或主动额度查询。

### OAuth 渠道架构

#### `OAuthManager`

`OAuthManager` 位于 `core/oauth/manager.py`，负责 OAuth 凭据的状态管理、token 刷新、额度缓存和持久化。

主要职责：

1. 维护分渠道状态：`{channel_id: {key_id: credential}}`。
2. 通过 `resolve(channel_id, key_id)` 把 key_id 解析为 `access_token`。
3. 在 token 过期前调用 provider 的 `refresh_token()`。
4. 通过 `fetch_quota(channel_id, key_id, force=False)` 查询或读取 OAuth 额度。
5. 通过 `update_quota()` 缓存 `quota_inner`、`quota_outer`、`quota_raw` 和 extra usage 字段。
6. 用延迟 flush 减少 quota 高频更新造成的磁盘写入。
7. 通过 `rename()`、`copy_channel_state()` 支持账号改名和渠道复制。

状态文件由 `core/oauth/state.py` 管理。运行时会把旧的扁平 state 自动迁移为分渠道结构。

#### `OAuthProvider`

`OAuthProvider` 位于 `core/oauth/providers/base.py`，是各 OAuth 渠道 provider 的基类。

基类接口：

```python
class OAuthProvider:
    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        ...

    def build_auth_url(self, state: str, redirect_uri: str) -> tuple[str, str]:
        ...

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        ...

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        return None

    def get_default_base_url(self) -> str:
        ...
```

说明：

1. `refresh_token()` 是必须实现的刷新入口。
2. `build_auth_url()` 和 `exchange_code()` 用于浏览器登录或手动粘贴授权码。
3. `fetch_quota()` 是可选能力。基类默认返回 `None`，只有实际能返回额度数据的渠道才覆盖它。
4. `config` 由 `OAuthManager` 按当前运行时配置注入，provider 不应缓存启动时的 `token_url` 或 `base_url`。

#### `ChannelDefinition.oauth_provider`

`ChannelDefinition.oauth_provider` 位于 `core/channels/registry.py`。渠道在 `register_channel()` 时可以传入 provider 实例。

```python
register_channel(
    id="codex",
    type_name="openai-responses",
    is_oauth=True,
    oauth_provider=CodexProvider(),
    ui_slots={
        "quota_display": CODEX_QUOTA_DISPLAY,
        "import_placeholder": "rt_xxxxxxxx...",
    },
)
```

约定：

1. 传入 `oauth_provider` 时，注册表会自动把 `is_oauth` 提升为 `True`。
2. `oauth_provider` 不会出现在 `ChannelDefinition.to_dict()` 的 API 输出中。
3. `ui_slots` 可以与 OAuth provider 同时注册，用于导入占位、额度展示和渠道提示。插槽规则见 [UI 插槽系统](#ui-slots)。

#### `set_oauth_provider_registrar`

`set_oauth_provider_registrar` 是注册表和 `OAuthManager` 之间的桥接函数。

工作方式：

1. `OAuthManager` 初始化时调用 `set_oauth_provider_registrar(self._register_channel_oauth_provider, replay_existing=True)`。
2. `replay_existing=True` 会把已注册渠道中声明的 provider 立即注册到 manager。
3. 后续插件热加载或渠道重新注册时，注册表会自动把新的 provider 同步给 manager。
4. 如果 provider 暴露 `set_oauth_manager()`，manager 会先注入自身，再调用 `register_provider()`。
5. 如果 provider 暴露 `set_config_getter()`，manager 会注入 `get_config()`，使 provider 能读取最新运行时配置。

该机制使插件 reload 时也能自动注册 OAuth provider，不需要在 `main.py` 中维护固定渠道清单。

### 已有 OAuth 渠道列表

| 渠道 | OAuth Provider | 认证方式 | `import_placeholder` |
|---|---|---|---|
| `codex` | `CodexProvider` | PKCE + OpenAI OAuth2 | `rt_xxxxxxxx...` |
| `claude-code` | `ClaudeCodeProvider` | Anthropic OAuth | `sk-ant-oat01-xxxxxxxx...` |
| `antigravity` | `AntigravityProvider` | Google OAuth | `1//0xxxxxxxx...` |
| `gemini-cli` | `GeminiCLIProvider` | Google OAuth | `1//0xxxxxxxx...` |
| `opal` | `OpalProvider` | Google OPAL cookie refresh | `1//0xxxxxxxx...` |
| `vertex-gemini` | `VertexProvider` | Service Account JSON | `{"type": "service_account", ...}` |
| `vertex-claude` | `VertexProvider` | Service Account JSON | `{"type": "service_account", ...}` |

说明：

1. `codex`、`claude-code`、`antigravity` 是当前内置且覆盖 `fetch_quota()` 的 OAuth 渠道，因此注册了 `quota_display`。
2. `gemini-cli` 当前没有实际额度查询实现，只注册 `import_placeholder`。
3. `vertex-gemini` 和 `vertex-claude` 使用服务账号 JSON 导入，当前没有实际额度查询实现，只注册 key、base_url、token_url 提示和 `import_placeholder`。
4. `opal` 是插件渠道形态，参考 `opal_channel.py`。当前 OPAL provider 的 `fetch_quota()` 返回 `None`，因此不需要注册 `quota_display`。

### 新建 OAuth 渠道模板

新建 OAuth 渠道时，可以参考 `codex_channel.py` 或 `opal_channel.py`。

#### 基本步骤

1. 新建渠道文件，例如 `core/channels/example_oauth_channel.py` 或插件文件 `plugins/example_oauth_channel.py`。
2. 实现一个继承 `OAuthProvider` 的 provider。
3. 实现 request adapter，把普通 Key 认证转换为 OAuth Bearer 认证。
4. 在 `register()` 或插件 `setup()` 中调用 `register_channel()`。
5. 传入 `oauth_provider=ExampleProvider()`。
6. 注册至少一个 `import_placeholder`。
7. 如果 provider 的 `fetch_quota()` 能返回额度数据，再注册 `quota_display`。

#### Provider 模板

```python
from core.oauth.providers.base import OAuthProvider


class ExampleProvider(OAuthProvider):
    redirect_mode = "manual"
    localhost_redirect_uri = "http://localhost:8080/callback"

    def __init__(self):
        self._config_getter = None

    def set_config_getter(self, config_getter):
        self._config_getter = config_getter

    def get_default_base_url(self) -> str:
        return "https://api.example.com/v1"

    def build_auth_url(self, state: str, redirect_uri: str) -> tuple[str, str]:
        return "https://auth.example.com/oauth", ""

    async def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None, config: dict | None = None) -> dict:
        raise NotImplementedError("example channel uses import mode")

    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        refresh_token = credential.get("refresh_token")
        if not refresh_token:
            raise ValueError("refresh_token is required")
        # 使用 refresh_token 换取 access_token，并保留原 credential 中的稳定身份字段。
        updated = dict(credential)
        updated["access_token"] = "new-access-token"
        return updated

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        # 只有实际能查到额度时才实现本方法并注册 quota_display。
        return {
            "quota_inner": 80.0,
            "quota_outer": 60.0,
            "raw": {"source": "example"},
        }
```

#### 渠道注册模板

```python
EXAMPLE_QUOTA_DISPLAY = """
export default function render(ctx) {
    const { el, data } = ctx || {};
    if (!el) return;
    const inner = typeof data?.quota_inner === 'number' ? data.quota_inner : null;
    const outer = typeof data?.quota_outer === 'number' ? data.quota_outer : null;
    const values = [inner, outer].filter(v => v != null);
    const pct = values.length ? Math.round(Math.min(...values)) : null;
    el.textContent = pct == null ? '' : pct + '%';
}
""".strip()


def register():
    from core.channels.registry import register_channel

    register_channel(
        id="example-oauth",
        type_name="openai",
        default_base_url="https://api.example.com/v1",
        auth_header="Authorization: Bearer {api_key}",
        request_adapter=get_example_payload,
        is_oauth=True,
        oauth_provider=ExampleProvider(),
        ui_slots={
            "quota_display": EXAMPLE_QUOTA_DISPLAY,
            "import_placeholder": "rt_xxxxxxxx...",
        },
    )
```

#### 从现有渠道参考

- 参考 `codex_channel.py`：适合需要 PKCE、OpenAI OAuth2、Responses API、被动采集 quota headers 的渠道。
- 参考 `opal_channel.py`：适合插件渠道、只支持导入 refresh token、不支持标准 code exchange 的渠道。

### OAuth 改名和复制

#### rename API

接口：`/v1/oauth/accounts/{key_id}/rename`

方法：`PUT`

请求体：

```json
{
  "provider": "Codex-Main",
  "new_key_id": "new-user@example.com"
}
```

行为：

1. 路由读取 `provider`，限定当前渠道。
2. 调用 `OAuthManager.rename(channel_id, old_key_id, new_key_id)`。
3. 在 `oauth_state.json` 中迁移账号 key。
4. 同步更新当前运行时配置里的 `api` 列表。
5. 通过 `_persist_config()` 写回 `api.yaml`。
6. 保留禁用前缀 `!` 和 dict 形式备注。

响应示例：

```json
{
  "message": "Account renamed",
  "old_key_id": "old-user@example.com",
  "new_key_id": "new-user@example.com"
}
```

错误状态：

- `400`：缺少 `provider` 或 `new_key_id`。
- `404`：旧账号不存在。
- `409`：新账号已经存在。

#### copy API

接口：`/v1/oauth/copy-provider`

方法：`POST`

请求体：

```json
{
  "source_provider": "Codex-Main",
  "target_provider": "Codex-Copy"
}
```

行为：

1. 路由校验 `source_provider` 和 `target_provider`。
2. 调用 `OAuthManager.copy_channel_state(source_provider, target_provider)`。
3. 从源渠道复制所有 OAuth 账号 state 到目标渠道。
4. 如果目标渠道已有同名账号，保留目标现有凭据，不覆盖。
5. 如有新增账号，持久化写回 `oauth_state.json`。

响应示例：

```json
{
  "message": "OAuth state copied",
  "source_provider": "Codex-Main",
  "target_provider": "Codex-Copy"
}
```

使用场景：复制渠道配置时，`api.yaml` 中的账号 key 会被复制，但 OAuth token state 不在 `api.yaml` 中。调用 copy API 后，新渠道可以继续使用源渠道账号对应的 OAuth state。

### OAuth 渠道的 UI 插槽要求

OAuth 渠道应遵守以下插槽规则。插槽注册和脚本规范详见 [UI 插槽系统](#ui-slots)。

1. 每个 OAuth 渠道都应提供 `import_placeholder`，用于提示导入凭据格式。
2. `import_placeholder` 是纯文本，不是 JS。
3. 如果 provider 覆盖 `fetch_quota()` 并能返回 `quota_inner`、`quota_outer` 或其他可展示额度，应注册简单的 `quota_display`。
4. 如果 provider 没有实际额度数据，跳过 `quota_display`。
5. 渠道专属背景、边框或提示应放入 `ui_slots`，不要写进通用前端。

当前内置渠道状态：

| 渠道 | `import_placeholder` | `quota_display` | 原因 |
|---|---:|---:|---|
| `codex` | 是 | 是 | `CodexProvider.fetch_quota()` 能解析 quota headers。 |
| `claude-code` | 是 | 是 | `ClaudeCodeProvider.fetch_quota()` 能读取 usage 端点。 |
| `antigravity` | 是 | 是 | `AntigravityProvider.fetch_quota()` 能读取模型组额度。 |
| `gemini-cli` | 是 | 否 | 当前没有实际额度查询实现。 |
| `opal` | 是 | 否 | 当前 `fetch_quota()` 返回 `None`。 |
| `vertex-gemini` | 是 | 否 | Service Account JSON 渠道当前没有额度查询实现。 |
| `vertex-claude` | 是 | 否 | Service Account JSON 渠道当前没有额度查询实现。 |

<a id="appendix-reference"></a>
## 附录/参考

### 插件管理器 API

#### 加载插件

```python
from core.plugins import get_plugin_manager

manager = get_plugin_manager()

# 加载所有插件
result = manager.load_all()

# 加载单个插件（文件路径）
info = manager.load_plugin("plugins/my_plugin.py")

# 加载单个插件（模块路径）
info = manager.load_plugin("my_package.my_plugin")
```

#### 卸载和重载

```python
# 卸载插件
manager.unload_plugin("my_plugin")

# 重载插件（热更新）
manager.reload_plugin("my_plugin")
```

#### 扩展管理

```python
# 获取扩展
extensions = manager.get_extensions("channels")

# 获取实现
implementations = manager.get_implementations("channels")

# 启用/禁用扩展
manager.enable_extension("channels", "my_channel")
manager.disable_extension("channels", "my_channel")

# 注销扩展
manager.unregister_extension("channels", "my_channel")
```

#### 状态查询

```python
# 获取系统状态
status = manager.get_status()
print(status)

# 获取所有插件
plugins = manager.plugins

# 获取扩展点列表
extension_points = manager.list_extension_points()
```

### 通过 pip 安装的插件

插件也可以打包为 Python 包并通过 pip 安装。

#### 创建插件包

```text
my_zoaholic_plugin/
├── pyproject.toml
├── my_plugin/
│   ├── __init__.py
│   └── channel.py
```

#### `pyproject.toml`

```toml
[project]
name = "my-zoaholic-plugin"
version = "1.0.0"

[project.entry-points."zoaholic.plugins"]
my_plugin = "my_plugin"
```

#### 安装和使用

```bash
pip install my-zoaholic-plugin
```

插件会在启动时自动通过 entry points 加载。

### 自定义扩展点

可以创建自己的扩展点：

```python
from core.plugins import ExtensionPoint, ExtensionPointType

def setup(manager):
    # 定义新的扩展点
    my_extension_point = ExtensionPoint(
        name="my_custom_point",
        type=ExtensionPointType.CUSTOM,
        description="我的自定义扩展点",
        required_methods=["process"],
        optional_methods=["validate"],
        singleton=False,
        priority_support=True,
    )

    # 注册扩展点
    manager.register_extension_point(my_extension_point)
```

### 最佳实践

#### 1. 错误处理

```python
def setup(manager):
    try:
        # 注册扩展
        manager.register_extension(...)
    except Exception as e:
        print(f"[{PLUGIN_INFO['name']}] 初始化失败: {e}")
        raise
```

#### 2. 资源清理

```python
_resources = []

def setup(manager):
    # 初始化资源
    _resources.append(create_resource())

def teardown(manager):
    # 清理资源
    for resource in _resources:
        resource.close()
    _resources.clear()
```

#### 3. 配置管理

```python
PLUGIN_CONFIG = {
    "api_url": "https://api.example.com",
    "timeout": 30,
}

def setup(manager):
    # 从环境变量覆盖配置
    import os
    if url := os.getenv("MY_PLUGIN_API_URL"):
        PLUGIN_CONFIG["api_url"] = url
```

#### 4. 日志记录

```python
from core.log_config import logger

def setup(manager):
    logger.info(f"[{PLUGIN_INFO['name']}] 正在初始化...")
```

### 完整示例

查看 `plugins/example_channel.py` 获取完整的渠道插件示例。OAuth 渠道可以参考 `core/channels/codex_channel.py` 或插件形态的 `opal_channel.py`。

### 常见问题

#### Q: 插件加载顺序？

A: 插件按文件名字母顺序加载。如需控制顺序，可以使用数字前缀命名，如 `01_first_plugin.py`。

#### Q: 如何处理插件依赖？

A: 在 `PLUGIN_INFO["dependencies"]` 中声明依赖，系统会检查依赖是否满足。

#### Q: 插件可以访问应用状态吗？

A: 可以，通过 `manager` 参数可以访问插件系统，但建议避免直接修改应用状态。

#### Q: 如何调试插件？

A: 设置环境变量 `DEBUG=True`，查看详细日志输出。
