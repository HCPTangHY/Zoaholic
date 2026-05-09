# Zoaholic OAuth 凭据管理技术文档（v1.6.0）

## 一、概述

Zoaholic v1.6.0 已实现订阅式 CLI 工具的原生 OAuth 凭据管理。当前内置 OAuth 渠道为 `claude-code`、`codex` 和 `gemini-cli`。用户完成 OAuth 登录或导入 refresh_token 后，运行时凭据保存到 `data/oauth_state.json`，渠道配置文件 `api.yaml` 只保存账号标识符。请求进入 handler 后统一把账号标识符解析为 access_token，再交给对应渠道 adapter 访问上游。

该功能的目标是让 OAuth 账号在路由、冷却、统计和管理端中继续表现为普通渠道的 key，同时避免 access_token、refresh_token 写入 `api.yaml` 或日志。

## 二、内置 OAuth 渠道

| 引擎 ID | type_name | 透传方言 | 上游默认地址 | Token URL 默认值 | 认证方式 |
|---|---|---|---|---|---|
| `claude-code` | `claude` | Claude 方言 | `https://api.anthropic.com` | `https://api.anthropic.com/v1/oauth/token` | `Authorization: Bearer {access_token}` |
| `codex` | `openai-responses` | OpenAI Responses API 方言 | `https://chatgpt.com/backend-api/codex` | `https://auth.openai.com/oauth/token` | `Authorization: Bearer {access_token}` |
| `gemini-cli` | `gemini` | Gemini 方言 | `https://cloudcode-pa.googleapis.com` | `https://oauth2.googleapis.com/token` | `Authorization: Bearer {access_token}` |

说明：

- `codex` 已注册为 `openai-responses`，请求与透传默认走 `/responses`，并强制 `store=false`。
- 原 `antigravity` 已改为 `gemini-cli`。当前核心渠道注册表中的正式引擎 ID 是 `gemini-cli`。
- 所有 OAuth 渠道注册时均声明 `is_oauth=True`，前端和余额路由通过该标记选择 OAuth 账号管理逻辑。

## 三、OAuth 常量

### 3.1 Codex

源码位置：`core/channels/codex_channel.py`。

| 项 | 已实现值 |
|---|---|
| Auth URL | `https://auth.openai.com/oauth/authorize` |
| Token URL | `https://auth.openai.com/oauth/token` |
| client_id | `app_EMoamEEZ73f0CkXaXp7hrann` |
| redirect_uri | `http://localhost:1455/auth/callback` |
| scopes | `openid email profile offline_access` |
| PKCE | S256，授权 URL 中带 `code_challenge` 和 `code_challenge_method=S256` |
| 登录模式 | `manual` |

Codex 授权 URL 还会携带 `prompt=login`、`id_token_add_organizations=true` 和 `codex_cli_simplified_flow=true`。授权码交换和 refresh 均使用 `application/x-www-form-urlencoded` 表单。id_token 会被不验签解码，用于提取 email 和 ChatGPT account_id。

### 3.2 Claude Code

源码位置：`core/channels/claude_code_channel.py`。

| 项 | 已实现值 |
|---|---|
| Auth URL | `https://claude.ai/oauth/authorize` |
| Token URL | `https://api.anthropic.com/v1/oauth/token` |
| client_id | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` |
| redirect_uri | `http://localhost:54545/callback` |
| scopes | `user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload` |
| PKCE | S256，授权码交换必须带 `code_verifier` |
| 登录模式 | `manual` |

Claude Code 的 token exchange 和 refresh 使用 JSON body。请求 adapter 会把普通 Claude adapter 生成的认证头改为 Bearer，并合并 Claude Code 所需的 `anthropic-beta`，其中包含 `oauth-2025-04-20`。当前常量还包含 Claude Code User-Agent 与 gzip magic-byte 兼容处理。

### 3.3 Gemini CLI

源码位置：`core/channels/gemini_cli_channel.py`。

| 项 | 已实现值 |
|---|---|
| Auth URL | `https://accounts.google.com/o/oauth2/v2/auth` |
| Token URL | `https://oauth2.googleapis.com/token` |
| client_id | 通过 base64 解码得到：`681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com` |
| client_secret | 通过 base64 解码得到，可由 `GEMINI_CLI_CLIENT_SECRET` 覆盖 |
| redirect_uri | `http://localhost:8085/oauth2callback` |
| scopes | `cloud-platform`、`userinfo.email`、`userinfo.profile` |
| PKCE | 不使用 PKCE，token 请求需要 `client_secret` |
| 登录模式 | `manual` |

Gemini CLI 使用 Google OAuth installed app 形式。授权码交换后会用 access_token 请求 Google userinfo，尽量以邮箱作为账号标识符。上游请求会转为 Cloud Code Assist `v1internal` 协议，并保留 `project_id` 支持。

## 四、配置与状态文件

### 4.1 api.yaml 只保存账号标识符

```yaml
- provider: "Claude-Code-Accounts"
  engine: claude-code
  base_url: "https://api.anthropic.com"
  token_url: "https://api.anthropic.com/v1/oauth/token"
  api:
    - "alice@example.com"
  model:
    - claude-sonnet-4

- provider: "Codex-Pool"
  engine: codex
  base_url: "https://chatgpt.com/backend-api/codex"
  token_url: "https://auth.openai.com"
  api:
    - "dev@example.com"
  model:
    - gpt-5.3-codex

- provider: "Gemini-CLI"
  engine: gemini-cli
  base_url: "https://cloudcode-pa.googleapis.com"
  token_url: "https://oauth2.googleapis.com"
  api:
    - "user@example.com"
  model:
    - gemini-2.5-pro
```

`api` 列表中的字符串是账号标识符。handler 会先尝试从 OAuthManager 中解析这些标识符；找不到时按普通静态 key 原样透传，以保持向后兼容。

### 4.2 data/oauth_state.json 保存运行时凭据

```json
{
  "dev@example.com": {
    "type": "codex",
    "access_token": "at_xxx",
    "refresh_token": "rt_xxx",
    "id_token": "eyJ...",
    "token_type": "Bearer",
    "expires_at": 1780000000,
    "email": "dev@example.com",
    "account_id": "acc_xxx",
    "status": "active",
    "last_refresh": "2026-05-09T12:00:00Z",
    "error_count": 0,
    "quota_5h": 83.4,
    "quota_7d": 64.2,
    "quota_raw": {}
  }
}
```

`/v1/oauth/accounts` 返回账号状态时会把 `access_token` 和 `refresh_token` 替换为 `***`，不会把明文 token 交给前端。

## 五、状态持久化与刷新保护

`core/oauth/state.py` 和 `core/oauth/manager.py` 已实现以下保护：

- 原子写入：同目录创建临时文件，写入 JSON 后执行 `flush`、`fsync`，再用 `os.replace` 替换正式文件。
- 文件权限：写入后尽量设置为 `0600`。
- 读取容错：`load_state()` 发现 JSON 损坏或根对象类型错误时，将原文件备份为 `.corrupt.<timestamp>`，并返回空状态让服务继续启动。
- refresh 成功但落盘失败时，OAuthManager 会把内存状态回滚到旧凭据，避免使用未成功持久化的新 refresh_token。
- refresh 请求失败时，账号会写入 `status=error`、`last_error`、`last_error_at`，并递增 `error_count`。
- 连续失败熔断：`error_count >= 5` 且最近 5 分钟内失败过时，`resolve()` 直接返回 `None`，handler 会把该账号视为不可用并尝试其他 key。
- quota 被动采集使用 30 秒批量落盘，避免普通请求频繁写入 `oauth_state.json`。

## 六、登录流程

### 6.1 路由

| 方法 | 路径 | 作用 | 鉴权 |
|---|---|---|---|
| GET | `/v1/oauth/authorize?type=...&origin=...` | 生成授权 URL，返回 `auth_url`、`state`、`mode` | admin |
| POST | `/v1/oauth/exchange` | manual 模式下用用户粘贴的 localhost 回调 URL 中的 code 换 token | admin |
| GET | `/v1/oauth/callback` | auto 模式下接收 provider 回调并注册账号 | state 校验 |
| POST | `/v1/oauth/import` | 手动导入 refresh_token 或完整 token 数据 | admin |
| GET | `/v1/oauth/accounts` | 列出 OAuth 账号状态 | admin |
| GET | `/v1/oauth/accounts/{key_id}/quota` | 查询单个 OAuth 账号额度 | admin |
| PUT | `/v1/oauth/accounts/{key_id}/rename` | 重命名账号标识符 | admin |
| DELETE | `/v1/oauth/accounts/{key_id}` | 删除 OAuth 账号 | admin |

state 保存在进程内 pending flow 中，有效期为 300 秒。callback 不要求 admin header，因为第三方 OAuth provider 的浏览器回跳无法携带管理端 Authorization header；安全边界由 state、过期时间和 pending flow 共同提供。

### 6.2 manual 模式

manual 模式用于 redirect_uri 固定为 localhost 的 provider。当前 Codex、Claude Code 和 Gemini CLI 均声明为 manual。

流程如下：

1. 前端调用 `/v1/oauth/authorize?type={engine}&origin={window.location.origin}`。
2. 后端按 provider 的 `localhost_redirect_uri` 构建授权 URL，并返回 `mode=manual`。
3. 用户在弹出的授权页完成登录，provider 跳转到 localhost 回调地址。
4. 用户复制浏览器中的完整回调 URL，粘贴到前端手动交换弹窗。
5. 前端解析 code 和 state，调用 `/v1/oauth/exchange`。
6. 后端换取 token，注册账号，返回 `key_id`。

### 6.3 auto 模式

auto 模式已在后端和前端实现，供声明 `redirect_mode="auto"` 的 provider 使用。

流程如下：

1. 前端发起 authorize 时传入 `origin=window.location.origin`。
2. 后端优先使用 `origin` 构建 `https://zoaholic.example/v1/oauth/callback` 形式的 redirect_uri；没有 origin 时回退到代理头和请求 Host。
3. provider 回跳 `/v1/oauth/callback` 后，后端完成 token exchange 并渲染成功页。
4. 成功页通过 `window.opener.postMessage({ type: "oauth_callback_success", key_id, state }, "*")` 通知管理前端。
5. 前端校验 state 后写入 key 行，并刷新 OAuth 账号列表。

## 七、token_url 运行时读取

OAuth token endpoint 不在应用启动时固化。`main.py` 在 lifespan 中调用 `OAuthManager.set_config_ref(lambda: app.state.config or {})`，OAuthManager 调用 provider 的 refresh 或 exchange 方法时注入当前配置。

各 provider 在每次 token 请求前解析当前配置中的 `token_url`：

- Codex：查找 `engine=codex` 的 provider，若填写根地址则补 `/oauth/token`；若已经包含 `/oauth/token` 则原样使用。
- Gemini CLI：查找 `engine=gemini-cli` 的 provider，支持 provider 顶层 `token_url` 和 `preferences.token_url`；若填写根地址则补 `/token`。
- Claude Code：使用 `DEFAULT_TOKEN_URL`，同时保留同一套 manager 注入调用路径。

前端渠道编辑面板已增加 Token URL 输入框。该输入框只在 `is_oauth` 渠道显示，保存时随 provider payload 写入，空字符串也会作为显式清空值提交。

## 八、渠道注册与请求解析架构

### 8.1 单文件渠道实现

每个 OAuth 渠道是一个自包含核心渠道文件：

- `core/channels/codex_channel.py`：Codex OAuth provider、Responses API 渠道 adapter、Codex quota 采集。
- `core/channels/claude_code_channel.py`：Claude Code OAuth provider、Claude adapter 包装、Claude Code 请求头和 gzip 兼容处理。
- `core/channels/gemini_cli_channel.py`：Gemini CLI OAuth provider、Google token exchange、Cloud Code Assist 协议适配。

`core/channels/__init__.py` 导入这些模块并调用各自的 `register()`。注册结果进入渠道注册表，管理端通过 `ChannelDefinition.to_dict()` 读取 `default_base_url`、`default_token_url`、`is_oauth` 和 `source`。

### 8.2 OAuth provider 注册

`main.py` lifespan 创建 `OAuthManager` 后调用：

- `codex_channel.register_oauth_provider(app.state.oauth_manager)`
- `claude_code_channel.register_oauth_provider(app.state.oauth_manager)`
- `gemini_cli_channel.register_oauth_provider(app.state.oauth_manager)`

这些函数只注册 provider 实例，不再从启动时 providers 参数中固化 token_url。

### 8.3 handler 统一解析 token

`core/handler.py` 中的 `_resolve_oauth_api_key(app, api_key)` 是请求路径的统一入口。

处理顺序为：

1. 现有轮询逻辑仍然从 provider 的 `api` 列表中取出账号标识符。
2. handler 先把原始标识符写入 request_info 的 `_used_api_key`，供日志、统计、冷却和 quota wrapper 使用。
3. `_resolve_oauth_api_key()` 调用 `app.state.oauth_manager.resolve(key_id)`。
4. resolve 成功时把 access_token 传给 channel adapter；resolve 失败或找不到账号时保留原 key。

因此 `.next()`、日志、冷却和统计系统看到的仍是配置中的 key_id，不会看到 access_token。

## 九、额度与余额查询

### 9.1 Codex quota 采集

Codex 已实现被动采集和主动查询两条路径。

被动采集：

- `fetch_codex_response()` 和 `fetch_codex_response_stream()` 用 `_QuotaCapturingClient` 包装 Responses API adapter。
- wrapper 从响应对象读取 headers，优先解析 `x-codex-primary-used-percent`、`x-codex-secondary-used-percent`，并 fallback 到 `x-ratelimit-*`。
- 解析结果写入 OAuthManager 内存中的 `quota_5h`、`quota_7d` 和 `quota_raw`，随后延迟批量落盘。

主动查询：

- `CodexProvider.fetch_quota()` 发一个轻量 Responses API 请求。
- 请求使用当前 access_token、当前配置中的 base_url、`stream=true`、`store=false`。
- 返回时仍从响应 headers 解析 quota。

Claude Code 和 Gemini CLI 当前未实现专门 quota 查询，`fetch_quota()` 返回 `None`。

### 9.2 前端展示

前端账号列表打开时先读取 `/v1/oauth/accounts`。对于 active 且没有 quota 缓存的账号，再异步调用 `/v1/oauth/accounts/{key_id}/quota`。

`QuotaBorderOverlay` 已实现边框进度条：

- 上半边蓝色表示 `quota_5h`。
- 下半边紫色表示 `quota_7d`。
- 没有 quota 数据时显示连接状态，不渲染进度边框。

### 9.3 余额路由分流

OAuth 账号额度不走普通 `core.balance` 查询逻辑。

- 账号管理面板按账号调用 `/v1/oauth/accounts/{key_id}/quota`。
- 渠道编辑面板中的“余额”按钮仍调用 `/v1/channels/balance`，但后端会先读取渠道注册表；若 `channel.is_oauth=True`，则调用 `OAuthManager.fetch_quota()` 并返回百分比结构，不会进入普通 `preferences.balance` 路径。

返回结构兼容普通 BalanceResult，同时保留 `quota_5h`、`quota_7d`、`raw` 和逐账号 `results`。

## 十、安全边界

- `api.yaml` 不保存 OAuth token。
- `oauth_state.json` 尽量以 `0600` 权限保存。
- OAuth 账号列表接口隐藏 access_token 和 refresh_token。
- 请求日志、统计、冷却和自动禁用逻辑使用 key_id，不记录 access_token。
- OAuth authorize pending flow 使用随机 state，并在 300 秒后过期。
- callback 只接受 pending flow 中存在且未过期的 state。
- refresh 连续失败后会短期熔断，避免坏 refresh_token 在请求路径中反复访问上游。
