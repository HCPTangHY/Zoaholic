# Zoaholic

<p align="center">
  <img src="static/zoaholic.png" alt="Zoaholic Logo" width="200"/>
</p>

<p align="center">
  <strong>统一大模型 API 网关</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> | <a href="./README_EN.md">English</a>
</p>

## 📖 介绍

Zoaholic 是一个基于 [uni-api](https://github.com/yym68686/uni-api) 二次开发的统一大模型 API 网关。面向高客制化的复杂需求，去除 new-api 复杂的商业功能。它通过一个统一的 API 接口调用多种不同提供商的服务，统一转换为 OpenAI 格式，支持负载均衡。

### 支持的后端服务

| 提供商 | 支持状态 | 说明 |
|--------|----------|------|
| OpenAI | ✅ | 包括 GPT-4o、DALL-E 3 等 |
| Anthropic | ✅ | Claude 系列模型 |
| Google Gemini | ✅ | Gemini 2.5 Pro/Flash 等 |
| Google Vertex AI | ✅ | 同时支持 Claude 和 Gemini |
| Azure OpenAI | ✅ | Azure 托管的 OpenAI 模型 |
| AWS Bedrock | ✅ | Claude 等模型 |
| 自定义插件 | ✅ | 通过插件系统扩展 |

## ✨ 特性

### 核心功能

- **纯配置文件驱动** - 只需编写一个 `api.yaml` 文件即可运行，小白友好
- **统一 API 格式** - 所有后端服务统一转换为 OpenAI 格式
- **内置管理前端** - Material Design 3 风格的可视化管理界面
- **插件系统** - 支持通过插件扩展渠道、中间件、钩子等

### 负载均衡

- **渠道级加权负载均衡** - 根据不同渠道权重分配请求
- **Vertex 区域级负载均衡** - 最高可将并发提高 (API数量 × 区域数量) 倍
- **渠道级顺序负载均衡** - 提高沉浸式翻译体验
- **API Key 级别轮询** - 单渠道多 Key 自动负载均衡

### 高可用

- **自动重试** - API 渠道响应失败时自动重试下一个渠道
- **渠道冷却** - 失败渠道自动排除，冷却后自动恢复
- **细粒度超时** - 可为每个模型设置不同的超时时间

### 安全与控制

- **细粒度权限控制** - 支持通配符设置 API Key 可用模型
- **限流** - 支持多种频率限制（分钟/小时/天/月/年）
- **道德审查** - 支持 OpenAI moderation 审查，降低封禁风险

### API 端点

- `/v1/chat/completions` - 聊天补全（OpenAI 兼容）
- `/v1/images/generations` - 图像生成
- `/v1/audio/transcriptions` - 音频转录
- `/v1/embeddings` - 文本嵌入
- `/v1/moderations` - 内容审核
- `/v1/models` - 模型列表
- `/v1beta/models` - Gemini 原生格式模型列表
- `/v1beta/models/{model}:generateContent` - Gemini 原生格式（非流式）
- `/v1beta/models/{model}:streamGenerateContent` - Gemini 原生格式（流式）
- `/v1/messages` - Claude 原生格式

## 🚀 快速开始

### 环境要求

- Python 3.11+
- 或 Docker

### 方法一：Docker 部署（推荐）

1. 创建配置文件 `api.yaml`：

```yaml
providers:
  - provider: openai
    base_url: https://api.openai.com/v1/chat/completions
    api: sk-your-api-key

api_keys:
  - api: sk-your-zoaholic-key
```

2. 使用 Docker Compose 启动：

```yaml
# docker-compose.yml
services:
  zoaholic:
    container_name: zoaholic
    image: zoaholic:latest
    ports:
      - 8000:8000
    volumes:
      - ./api.yaml:/home/api.yaml
      - ./data:/home/data
```

```bash
docker-compose up -d
```

### 方法二：本地运行

```bash
# 克隆项目
git clone https://github.com/your-repo/zoaholic.git
cd zoaholic

# 安装依赖（推荐使用 uv）
uv sync

# 创建配置文件
cp api.yaml.example api.yaml
# 编辑 api.yaml 配置你的 API

# 启动服务
python main.py

# 开发模式（启用热重载）
RELOAD=true python main.py
# 或使用 uvicorn 命令
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### 方法三：使用 CONFIG_URL

通过环境变量指定远程配置文件：

```bash
export CONFIG_URL=https://your-server.com/api.yaml
python main.py
```

## 📝 配置指南

### 最小配置

```yaml
providers:
  - provider: openai
    base_url: https://api.openai.com/v1/chat/completions
    api: sk-your-api-key

api_keys:
  - api: sk-your-zoaholic-key
```

### 完整配置示例

```yaml
providers:
  # OpenAI 格式提供商
  - provider: openai
    base_url: https://api.openai.com/v1/chat/completions
    api: sk-xxx
    model:
      - gpt-4o
      - gpt-4o-mini
      - dall-e-3

  # Anthropic Claude
  - provider: anthropic
    base_url: https://api.anthropic.com/v1/messages
    api:
      - sk-ant-api03-xxx  # 支持多 Key 轮询
      - sk-ant-api03-yyy
    model:
      - claude-3-5-sonnet-20240620: claude-3-5-sonnet  # 重命名模型
      - claude-3-7-sonnet-20250219: claude-3-7-sonnet-think
    tools: true
    preferences:
      api_key_rate_limit: 15/min
      api_key_cooldown_period: 60

  # Google Gemini
  - provider: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta
    api:
      - AIzaSyxxx
      - AIzaSyyyy
    model:
      - gemini-2.5-pro
      - gemini-2.5-flash
      - gemini-2.5-pro: gemini-2.5-pro-search  # 搜索版本
    tools: true
    preferences:
      api_key_rate_limit: 15/min
      model_timeout:
        gemini-2.5-pro: 500
        default: 100
      post_body_parameter_overrides:
        gemini-2.5-pro-search:
          tools:
            - google_search: {}

  # Google Vertex AI
  - provider: vertex
    project_id: your-project-id
    private_key: "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
    client_email: xxx@xxx.iam.gserviceaccount.com
    model:
      - gemini-2.5-flash
      - claude-3-5-sonnet@20240620: claude-3-5-sonnet
    tools: true

  # Azure OpenAI
  - provider: azure
    base_url: https://your-endpoint.openai.azure.com
    api: your-api-key
    model:
      - gpt-4o

  # AWS Bedrock
  - provider: aws
    base_url: https://bedrock-runtime.us-east-1.amazonaws.com
    aws_access_key: xxx
    aws_secret_key: xxx
    model:
      - anthropic.claude-3-5-sonnet-20240620-v1:0: claude-3-5-sonnet

  # Cloudflare Workers AI
  - provider: cloudflare
    api: your-cf-api-key
    cf_account_id: your-account-id
    model:
      - '@cf/meta/llama-3.1-8b-instruct': llama-3.1-8b

api_keys:
  # 管理员 Key
  - api: sk-admin-key
    role: admin
    model:
      - gpt-4o
      - claude-3-5-sonnet
      - gemini/*  # 通配符：gemini 渠道的所有模型
    preferences:
      SCHEDULING_ALGORITHM: round_robin
      AUTO_RETRY: true
      rate_limit: 60/min

  # 普通用户 Key
  - api: sk-user-key
    model:
      - gcp1/*
      - gcp2/*
      - gcp3/*
    preferences:
      SCHEDULING_ALGORITHM: weighted_round_robin  # 配合渠道 weight 使用
      credits: 10  # 余额限制
      created_at: 2024-01-01T00:00:00+08:00

# 全局配置
preferences:
  model_timeout:
    gpt-4o: 30
    claude-3-5-sonnet: 60
    default: 100
  cooldown_period: 300
  rate_limit: 999999/min
  error_triggers:
    - "The bot's usage is covered by the developer"
  model_price:
    gpt-4o: 2.5,10
    claude-3-5-sonnet: 3,15
    default: 1,2
```


### 高级完整配置示例（保留原版全部高级字段）

下面给出一个更完整的 [`api.yaml`](api.yaml:1) 高级配置示例，包含多提供商、多 API Key、频率限制、冷却、代理、价格配置等，基本覆盖原版 README 中的所有高级字段，便于直接对照修改：

```yaml
providers:
  - provider: provider_name # 服务提供商名称, 如 openai、anthropic、gemini、openrouter，随便取名字，必填
    base_url: https://api.your.com/v1/chat/completions # 后端服务的API地址，必填
    api: sk-YgS6GTi0b4bEabc4C # 提供商的API Key，必填
    model: # 选填，如果不配置 model，会自动通过 base_url 和 api 通过 /v1/models 端点获取可用的所有模型。
      - gpt-4o # 可以使用的模型名称，必填
      - claude-3-5-sonnet-20240620: claude-3-5-sonnet # 重命名模型，可以用简短别名
      - dall-e-3

  - provider: anthropic
    base_url: https://api.anthropic.com/v1/messages
    api: # 支持多个 API Key，多个 key 自动开启轮询负载均衡，至少一个 key，必填
      - sk-ant-api03-bNnAOJyA-xQw_twAA
      - sk-ant-api02-bNnxxxx
    model:
      - claude-3-7-sonnet-20240620: claude-3-7-sonnet
      - claude-3-7-sonnet-20250219: claude-3-7-sonnet-think # 带 think 的别名会被视为思考模型
    tools: true # 是否支持工具调用，默认 true
    preferences:
      post_body_parameter_overrides: # 针对模型自定义请求体参数
        claude-3-7-sonnet-think:
          tools:
            - type: code_execution_20250522
              name: code_execution
            - type: web_search_20250305
              name: web_search
              max_uses: 5

  - provider: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta # 仅供 Gemini 模型使用，必填
    api:
      - AIzaSyAN2k6IRdgw123
      - AIzaSyAN2k6IRdgw456
      - AIzaSyAN2k6IRdgw789
    model:
      - gemini-2.5-pro
      - gemini-2.5-flash: gemini-2.5-flash # 重命名后，如果还想用原名，需要再写一行原名
      - gemini-2.5-flash
      - gemini-2.5-pro: gemini-2.5-pro-search # 以 -search 结尾可作为“搜索版”模型
      - gemini-2.5-flash: gemini-2.5-flash-think-24576-search # 同时支持 search 与 think-数字
      - gemini-2.5-flash: gemini-2.5-flash-think-0 # think-0 表示关闭推理
      - gemini-embedding-001
      - text-embedding-004
    tools: true
    preferences:
      api_key_rate_limit: 15/min # 每个 API Key 每分钟最多请求次数，支持 15/min,10/day 等组合
      # api_key_rate_limit:
      #   gemini-2.5-flash: 10/min,500/day
      #   gemini-2.5-pro: 5/min,25/day,1048576/tpr # tpr 表示每次请求 tokens 上限
      #   default: 4/min
      api_key_cooldown_period: 60 # 单个 API Key 遭遇 429 后的冷却时间（秒）
      api_key_schedule_algorithm: round_robin # 多 Key 调度：round_robin/random/fixed_priority/smart_round_robin
      model_timeout: # 模型超时时间（秒），默认 100 秒
        gemini-2.5-pro: 500
        gemini-2.5-flash: 500
        default: 10
      keepalive_interval: # 心跳间隔（秒），适合 Cloudflare 场景
        gemini-2.5-pro: 50 # 必须小于 model_timeout
      proxy: socks5://[用户名]:[密码]@[IP地址]:[端口] # 支持 socks5 / http 代理
      headers:
        Custom-Header-1: Value-1
        Custom-Header-2: Value-2
      post_body_parameter_overrides:
        gemini-2.5-pro-search:
          tools:
            - google_search: {}
            - url_context: {}

  - provider: vertex
    project_id: gen-lang-client-xxxxxxxxxxxxxx # Google Cloud 项目 ID
    private_key: "-----BEGIN PRIVATE KEY-----\nxxxxx\n-----END PRIVATE KEY-----" # 服务账号私钥 JSON 中的 private_key
    client_email: xxxxxxxxxx@xxxxxxx.gserviceaccount.com # 服务账号邮箱
    model:
      - gemini-2.5-flash
      - gemini-2.5-pro
      - gemini-2.5-pro: gemini-2.5-pro-search
      - claude-3-5-sonnet@20240620: claude-3-5-sonnet
      - claude-3-opus@20240229: claude-3-opus
      - claude-3-sonnet@20240229: claude-3-sonnet
      - claude-3-haiku@20240307: claude-3-haiku
      - gemini-embedding-001
      - text-embedding-004
    tools: true
    notes: https://xxxxx.com/ # 备注、官方文档链接等
    preferences:
      post_body_parameter_overrides:
        gemini-2.5-pro-search:
          tools:
            - google_search: {}
        gemini-2.5-flash:
          generationConfig:
            thinkingConfig:
              includeThoughts: True
              thinkingBudget: 24576
            maxOutputTokens: 65535
        gemini-2.5-flash-search:
          tools:
            - google_search: {}
            - url_context: {}

  - provider: cloudflare
    api: f42b3xxxxxxxxxxq4aoGAh # Cloudflare API Key
    cf_account_id: 8ec0xxxxxxxxxxxxe721 # Cloudflare Account ID
    model:
      - '@cf/meta/llama-3.1-8b-instruct': llama-3.1-8b # 必须使用引号
      - '@cf/meta/llama-3.1-8b-instruct'

  - provider: azure
    base_url: https://your-endpoint.openai.azure.com
    api: your-api-key
    model:
      - gpt-4o
    preferences:
      post_body_parameter_overrides:
        key1: value1
        key2: value2
        stream_options:
          include_usage: true
      cooldown_period: 0 # 渠道级冷却设为 0 表示不启用冷却机制

  - provider: databricks
    base_url: https://xxx.azuredatabricks.net
    api:
      - xxx
    model:
      - databricks-claude-sonnet-4: claude-sonnet-4
      - databricks-claude-opus-4: claude-opus-4
      - databricks-claude-3-7-sonnet: claude-3-7-sonnet

  - provider: aws
    base_url: https://bedrock-runtime.us-east-1.amazonaws.com
    aws_access_key: xxxxxxxx
    aws_secret_key: xxxxxxxx
    model:
      - anthropic.claude-3-5-sonnet-20240620-v1:0: claude-3-5-sonnet

  - provider: vertex-express
    base_url: https://aiplatform.googleapis.com/
    project_id:
      - xxx # key1 的 project_id
      - xxx # key2 的 project_id
    api:
      - xx.xxx # key1 的 api
      - xx.xxx # key2 的 api
    model:
      - gemini-2.5-pro-preview-06-05

  - provider: other-provider
    base_url: https://api.xxx.com/v1/messages
    api: sk-bNnAOJyA-xQw_twAA
    model:
      - causallm-35b-beta2ep-q6k: causallm-35b
      - anthropic/claude-3-5-sonnet
    tools: false
    engine: openrouter # 强制使用 openrouter / gpt / claude / gemini 等特定消息格式

api_keys:
  - api: sk-KjjI60Yf0JFWxfgRmXqFWyGtWUd9GZnmi3KlvowmRWpWpQRo # API Key，必填
    model: # 该 Key 可用的模型列表，默认按此顺序轮询
      - gpt-4o
      - claude-3-5-sonnet
      - gemini/* # 仅使用 provider 名为 gemini 的所有模型
    role: admin # admin Key 可访问 /v1/stats,/v1/generate-api-key 等敏感接口

  - api: sk-pkhf60Yf0JGyJxgRmXqFQyTgWUd9GZnmi3KlvowmRWpWqrhy
    model:
      - anthropic/claude-3-5-sonnet # 仅 anthropic 渠道下的 claude-3-5-sonnet
      - <anthropic/claude-3-5-sonnet> # 尖括号包裹时，不再拆分 provider/name，而是整个匹配模型名
      - openai-test/omni-moderation-latest # 道德审查模型
      - sk-KjjI60Yd0JFWtxxxxxxxxxxxxxxwmRWpWpQRo/* # 可将其他 API Key 作为“渠道”引用
    preferences:
      SCHEDULING_ALGORITHM: fixed_priority # 固定优先级调度，始终优先第一个可用渠道
      # 可选：fixed_priority / round_robin / weighted_round_robin / lottery / random / smart_round_robin
      AUTO_RETRY: true # 自动重试下一个提供商，也可以设为数字表示重试次数
      rate_limit: 15/min # 限流支持多种周期：2/min,5/hour,10/day,10/month,10/year
      # rate_limit:
      #   gemini-2.5-flash: 10/min,500/day
      #   gemini-2.5-pro: 5/min,25/day
      #   default: 4/min
      ENABLE_MODERATION: true # 是否开启消息道德审查

  # 渠道级加权负载均衡示例
  # 权重在渠道配置的 preferences.weight 中设置，然后在 API Key 使用 weighted_round_robin 调度
  - api: sk-KjjI60Yd0JFWtxxxxxxxxxxxxxxwmRWpWpQRo
    model:
      - gcp1/*  # 渠道 gcp1 需设置 preferences.weight: 5
      - gcp2/*  # 渠道 gcp2 需设置 preferences.weight: 3
      - gcp3/*  # 渠道 gcp3 需设置 preferences.weight: 2
    preferences:
      SCHEDULING_ALGORITHM: weighted_round_robin # 基于渠道权重的轮询算法，或 lottery 抽奖式调度
      AUTO_RETRY: true
      credits: 10 # 余额上限（美元），为 0 表示不可使用
      created_at: 2024-01-01T00:00:00+08:00 # 费用统计起始时间

preferences: # 全局配置
  model_timeout: # 模型超时时间（秒），默认 100 秒
    gpt-4o: 10 # 对以 gpt-4o 开头的模型都生效
    claude-3-5-sonnet: 10
    default: 10 # 不设置 default 时，会退化为使用环境变量 TIMEOUT
    o1-mini: 30
    o1-preview: 100
  cooldown_period: 300 # 渠道级冷却时间（秒），设为 0 表示不启用冷却机制
  rate_limit: 999999/min # 全局速率限制，支持 15/min,10/day 等组合
  max_retry_count: 10 # 多渠道场景下的最大重试次数上限，默认 10，设置范围 1-100
  keepalive_interval: # 心跳间隔（秒），适合长推理场景
    gemini-2.5-pro: 50
  log_raw_data_retention_hours: 24 # 日志原始数据保留时间（小时），设为 0 或不设置表示不保存请求/响应原始数据
  error_triggers: # 错误触发器，响应文本包含任意字符串时视为错误并触发冷却
    - The bot's usage is covered by the developer
    - process this request due to overload or policy
  proxy: socks5://[username]:[password]@[ip]:[port] # 全局代理地址
  model_price: # 模型价格（美元 / 百万 tokens），格式：输入单价,输出单价
    gpt-4o: 1,2
    claude-3-5-sonnet: 0.12,0.48
    default: 1,2
```

### 调度算法说明

| 算法 | 说明 |
|------|------|
| `fixed_priority` | 固定优先级，始终使用第一个可用渠道（默认） |
| `round_robin` | 轮询负载均衡，按顺序依次请求 |
| `weighted_round_robin` | 加权轮询，按权重顺序请求 |
| `lottery` | 抽奖式，按权重随机请求 |
| `random` | 完全随机 |
| `smart_round_robin` | 智能调度，基于历史成功率排序 |

## 🔌 插件系统

Zoaholic 支持通过插件扩展功能。插件可以添加新的渠道适配器、中间件、钩子等。

### 创建插件

在 `plugins/` 目录下创建 Python 文件：

```python
# plugins/my_channel.py

PLUGIN_INFO = {
    "name": "my_channel",
    "version": "1.0.0",
    "description": "我的自定义渠道",
}

class MyChannelAdapter:
    id = "my_channel"
    type_name = "my_provider"
    
    @staticmethod
    async def request_adapter(request, engine, provider, api_key=None):
        url = provider.get('base_url')
        headers = {'Authorization': f'Bearer {api_key}'}
        payload = {...}
        return url, headers, payload
    
    @staticmethod
    async def stream_adapter(client, url, headers, payload, engine, model, timeout):
        async with client.stream('POST', url, ...) as response:
            async for line in response.aiter_lines():
                yield f"data: {line}\n\n"
        yield "data: [DONE]\n\n"

def setup(manager):
    from core.channels.registry import register_channel
    register_channel(
        id="my_channel",
        type_name="my_provider",
        request_adapter=MyChannelAdapter.request_adapter,
        stream_adapter=MyChannelAdapter.stream_adapter,
    )
```

详细文档请参阅 [插件开发指南](docs/plugin-development.md)。

## 🖥️ 管理前端

Zoaholic 内置了一个 Material Design 3 风格的管理前端，提供：

- **控制台总览** - 查看系统状态和统计信息
- **聊天测试** - 在线测试 API 调用
- **渠道管理** - 查看和管理 API 渠道
- **配置编辑** - 可视化编辑配置文件

访问 `http://localhost:8000/` 即可使用前端界面。

## ❓ 常见问题

### 为什么出现 "No matching model found" 错误？

将 `ENABLE_MODERATION` 设置为 `false`。当开启道德审查时，需要配置 `omni-moderation-latest` 模型。

### 如何设置渠道优先级？

在 `api_keys` 中按顺序配置模型即可：

```yaml
api_keys:
  - api: sk-xxx
    model:
      - ai2/*  # 优先请求
      - ai1/*  # 备用
```

### 如何正确填写 base_url？

- OpenAI 格式：必须以 `/v1/chat/completions` 结尾
- Azure：支持 `https://your-endpoint.openai.azure.com` 或完整 URL
- Gemini：使用 `https://generativelanguage.googleapis.com/v1beta`

### 模型超时时间优先级？

渠道级别 > 全局配置 > 环境变量 `TIMEOUT`（默认 100 秒）

### 什么时候 API Key 具有管理权限？

1. 只有一个 Key 时，自动获得管理权限
2. 多个 Key 时，需要设置 `role: admin`

## 🔧 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PORT` | 服务端口 | 8000 |
| `TIMEOUT` | 默认超时时间（秒） | 100 |
| `DEBUG` | 调试模式 | false |
| `RELOAD` | 启用热重载（开发模式） | false |
| `CONFIG_URL` | 远程配置文件 URL | - |
| `DISABLE_DATABASE` | 禁用数据库 | false |

## 📁 项目结构

```
zoaholic/
├── main.py              # 应用入口
├── api.yaml             # 配置文件
├── core/                # 核心模块
│   ├── channels/        # 渠道适配器
│   ├── plugins/         # 插件系统
│   ├── handler.py       # 请求处理
│   ├── routing.py       # 路由逻辑
│   └── ...
├── routes/              # API 路由
│   ├── chat.py          # 聊天接口
│   ├── models.py        # 模型接口
│   └── ...
├── plugins/             # 用户插件目录
├── static/              # 前端静态文件
└── docs/                # 文档
```

## 🤝 致谢

- [uni-api](https://github.com/yym68686/uni-api) - 本项目的技术基础

## 📄 许可证

MIT License
