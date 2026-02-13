# Zoaholic

<p align="center">
  <img src="frontend/public/zoaholic.png" alt="Zoaholic Logo" width="200"/>
</p>

<p align="center">
  <strong>多方言（OpenAI / Claude / Gemini）大模型 API 网关 + 可视化管理控制台</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> | <a href="./README_EN.md">English</a>
</p>

## 这是什么

Zoaholic 是一个基于 [uni-api](https://github.com/yym68686/uni-api) 二次开发的下一代大模型 API 网关。面向高客制化的复杂需求，去除复杂的商业计费功能。

随着大模型生态的发展，不再是 OpenAI 协议一统天下。Zoaholic 引入了**多方言（Multi-Dialect）架构**，原生理解并支持 OpenAI、Anthropic Claude 和 Google Gemini 三大主流 API 协议的双向转换与负载均衡。

Zoaholic支持以下服务：

- 多方言协议：OpenAI / Claude / Gemini 请求格式双向转换、统一路由、流式 SSE 转发
- 渠道与 Key 管理：前端控制台可视化配置 providers / api_keys
- 统计与日志：请求统计、日志查询（可选数据库）
- 插件系统：通过拦截器扩展请求/响应处理能力

## ✨ 核心特性

### 🗣️ 多方言网关 (Multi-Dialect)
Zoaholic 不再强迫所有请求转换为 OpenAI 格式。网关内置了智能路由：
- 请求 `/v1/chat/completions` (OpenAI 协议) 可以无缝转发给 Claude 或 Gemini 后端。
- 请求 `/v1/messages` (Claude 协议) 可以无缝转发给 OpenAI 或 Gemini 后端。
- 请求 `/v1beta/models/...` (Gemini 协议) 同理。
- 支持流式响应 (SSE) 的协议级双向转换。

### 🔌 动态插件系统 (Plugins)
基于 Python 的热插拔插件系统，通过拦截器机制，不修改核心代码即可扩展网关能力。内置特色插件：
- `claude_thinking`: 将 Claude 模型请求（后缀 `-thinking`）自动转换为带有 `<thinking>` 预填充的推理流，并在响应流中正确分离 `reasoning_content` 和普通 `content`。
- `gemini_empty_retry`: 解决 Gemini 偶尔返回空响应的问题。
- `claude_tools`: 增强 Claude 的函数调用能力。

### ⚖️ 企业级负载均衡
继承自 uni-api 的强大核心引擎（`core/routing.py`）：
- **调度算法**：支持固定优先级、轮询、加权轮询、抽奖和智能路由调度。
- **高可用**：渠道自动重试、冷却机制（Cooldown）、细粒度模型超时控制。
- **限流与并发**：基于 `ThreadSafeCircularList` 的高性能本地限流器。

---

## 快速开始（推荐：Docker + 初始化向导）

### 1）准备数据库（线上强烈推荐 PostgreSQL / Cloudflare D1）

Render / Aiven / Railway 等平台通常会提供 `DATABASE_URL`。

如果你在 Cloudflare Workers 部署，也可以直接使用 D1：

- `DB_TYPE=d1`
- `D1_ACCOUNT_ID`（或 `CF_ACCOUNT_ID`）
- `D1_DATABASE_ID`
- `D1_API_TOKEN`（或 `CF_API_TOKEN`，需具备 D1 Query 权限）

### 2）启动服务

> 下面以 GHCR 镜像为例，若自己构建镜像也同理。

```bash
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require" \
  ghcr.io/qianzhuowo/zoaholic:latest
```

### 3）首次初始化

打开浏览器：

- `http://localhost:8000/setup`

按页面提示设置 **管理员用户名/密码**。初始化完成后进入 `/login`，用账号密码登录。

初始化后你需要在控制台里配置：

- `providers`：上游渠道（OpenAI/Claude/Gemini/Azure/Vertex/Bedrock…）
- `api_keys`：你的网关访问 Key（给调用方使用）

> 说明：网关的 `/v1/*` OpenAI 兼容接口仍然使用 API Key；管理控制台使用“账号密码 + JWT”。

---

## 线上部署（Render 等）需要填哪些环境变量？

下面列的是“线上部署最常用、最容易踩坑的变量”。

### 必填（强烈建议）

| 变量 | 示例 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql://...` 或 `postgres://...` | PostgreSQL 连接串（与 D1 二选一）。统计/日志 + 配置入库都依赖数据库。 |

### 建议配置

| 变量 | 默认 | 说明 |
|---|---:|---|
| `CONFIG_STORAGE` | `file` | 配置来源策略：`auto\|db\|file\|url`。默认 `file`（`api.yaml` 为权威配置源）；云平台可用 `auto`（文件权威并同步写入 DB）或 `db`（DB 权威）。 |
| `SYNC_CONFIG_TO_FILE` | `false` | 是否把配置同步写回 `api.yaml`。线上通常文件系统只读/临时，建议保持 `false`。 |
| `JWT_SECRET` | （可选） | 管理控制台 JWT 签名密钥。**不设置也能用**：首次 `/setup` 会自动生成并持久化到 DB（`admin_user.jwt_secret`），后续重启复用。出于安全考虑仍建议在部署阶段直接设置环境变量。 |
| `DISABLE_DATABASE` | `false` | 是否关闭数据库。线上一般不要关（否则无法配置入库/无法统计）。 |

### Cloudflare D1（可选）

| 变量 | 默认 | 说明 |
|---|---:|---|
| `DB_TYPE` | `sqlite` | 设为 `d1` 启用 Cloudflare D1 HTTP 模式。 |
| `D1_ACCOUNT_ID` / `CF_ACCOUNT_ID` | - | Cloudflare Account ID。 |
| `D1_DATABASE_ID` | - | D1 数据库 ID。 |
| `D1_API_TOKEN` / `CF_API_TOKEN` | - | Cloudflare API Token（需 D1 Query 权限）。 |
| `D1_API_BASE_URL` | `https://api.cloudflare.com/client/v4` | D1 API 基础地址（一般无需改）。 |
| `D1_TIMEOUT_SECONDS` | `30` | D1 HTTP 请求超时秒数。 |

### 可选（高级用法 / 非必须）

| 变量 | 示例 | 说明 |
|---|---|---|
| `CONFIG_YAML` | YAML 文本 | 直接用环境变量提供配置种子。 |
| `CONFIG_YAML_BASE64` | base64(YAML) | 推荐方式：把 `api.yaml` base64 后放这里，首次启动会写入 DB。 |
| `CONFIG_URL` | `https://.../api.yaml` | 从 URL 拉取配置种子（首次写入 DB）。 |
| `ADMIN_API_KEY` / `ADMIN_API_KEYS` | `sk-...` | 当没有任何配置来源时，生成一个“最小可启动配置”（只含管理员 key），便于先启动再进控制台完善配置。 |
| `DEBUG` | `true/false` | 开启调试日志。 |

---

## 配置与持久化（重点：配置入库）

Zoaholic 支持把配置（原 `api.yaml`）持久化到数据库：

- 默认 `CONFIG_STORAGE=file`
  - 启动时优先从 `api.yaml` 读取（`api.yaml` 作为权威配置）
  - 前端保存配置会写回 `api.yaml`（需要文件系统可写/挂载卷）

- 可选 `CONFIG_STORAGE=auto`（云平台/多实例场景，仍以文件为权威）
  - 启动优先从 `api.yaml` / `CONFIG_YAML(_BASE64)` / `CONFIG_URL` 读取
  - 随后把配置写入 DB（便于备份/多实例共享），但不会反向覆盖 `api.yaml`

- 可选 `CONFIG_STORAGE=db`（云平台/多实例场景，以 DB 为权威）
  - DB 有配置：启动优先从 DB 读取
  - DB 无配置：会从 `CONFIG_YAML(_BASE64)` / `CONFIG_URL` / `api.yaml` 读取一次作为“种子”，并写入 DB

配置入库后：

- `app_config.config_json` 为主存储（PostgreSQL 使用 JSONB）
- `app_config.config_yaml` 仅作为可选导出/排查

---

## 本地开发（非 Docker）

```bash
# 后端
python -m venv .venv
# 激活虚拟环境后安装依赖
pip install -r requirements.txt

# 进入前端目录构建 UI
cd frontend && npm install && npm run build && cd ..

# 启动 FastAPI 服务
python main.py
```

---

## 常见问题

### 1）为什么服务启动后 /v1 接口 403？

`/v1/*` 是网关接口，默认必须带 API Key（`Authorization: Bearer sk-...` 或 `x-api-key`）。
请先在控制台配置 `api_keys`。

### 2）我不想填 JWT_SECRET 行不行？

可以。首次 `/setup` 会自动生成并把 `jwt_secret` 存到数据库，后续重启复用。
建议在云平台环境变量里显式设置 `JWT_SECRET`。

---

## 🤝 致谢

- [uni-api](https://github.com/yym68686/uni-api) - 本项目的优秀上游基础

## 🛠️ 开发工具

本项目使用 [Lim Code](https://github.com/Lianues/Lim-Code) 进行开发。