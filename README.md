# Zoaholic

<p align="center">
  <img src="frontend/public/zoaholic.png" alt="Zoaholic Logo" width="200"/>
</p>

<p align="center">
  <strong>下一代多方言大模型 API 网关</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> | <a href="./README_EN.md">English</a>
</p>

## 📖 介绍

Zoaholic 是一个基于 [uni-api](https://github.com/yym68686/uni-api) 二次开发的下一代大模型 API 网关。面向高客制化的复杂需求，去除复杂的商业计费功能。

随着大模型生态的发展，不再是 OpenAI 协议一统天下。Zoaholic 引入了**多方言（Multi-Dialect）架构**，原生理解并支持 OpenAI、Anthropic Claude 和 Google Gemini 三大主流 API 协议的双向转换与负载均衡。

### 支持的后端服务

| 提供商 | 支持状态 | 说明 |
|--------|----------|------|
| OpenAI | ✅ | 包括 GPT-4o、o1/o3-mini 等推理模型 |
| Anthropic | ✅ | Claude 3.5/3.7 系列模型，原生支持 Prompt Caching |
| Google Gemini | ✅ | Gemini 2.0/2.5 Pro/Flash 等 |
| Google Vertex AI | ✅ | 同时支持 Claude 和 Gemini |
| Azure OpenAI | ✅ | Azure 托管的 OpenAI 模型 |
| AWS Bedrock | ✅ | 支持 Claude 等模型 |
| Cloudflare | ✅ | Cloudflare Workers AI 等开源模型 |
| OpenRouter | ✅ | 支持通过 OpenRouter 接入丰富开源模型 |
| 自定义插件 | ✅ | 通过插件系统无限扩展渠道适配器 |

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

### 🖥️ 现代化 React 前端
内置基于 Vite + React + Tailwind CSS + Radix UI + Zustand 构建的 Material Design 风格管理控制台（`frontend/` 目录），提供可视化的渠道管理、配置查看和模型测试环境。

### ⚖️ 企业级负载均衡
继承自 uni-api 的强大核心引擎（`core/routing.py`）：
- **调度算法**：支持固定优先级、轮询、加权轮询、抽奖和智能路由调度。
- **高可用**：渠道自动重试、冷却机制（Cooldown）、细粒度模型超时控制。
- **限流与并发**：基于 `ThreadSafeCircularList` 的高性能本地限流器。

## 🚀 快速开始

### 环境要求
- Python 3.11+
- 或 Docker (推荐)

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

2. 启动容器：

```bash
docker run -d \
  --name zoaholic \
  -p 8000:8000 \
  -v ./api.yaml:/home/api.yaml \
  zoaholic:latest
```

访问 `http://localhost:8000/` 进入控制台。

### 方法二：本地开发

```bash
# 克隆项目
git clone https://github.com/your-repo/zoaholic.git
cd zoaholic

# 安装后端依赖 (推荐使用 uv 管理 pyproject.toml)
uv sync

# 进入前端目录构建 UI
cd frontend && npm install && npm run build && cd ..

# 启动 FastAPI 服务
python main.py
```

## 📁 项目架构

Zoaholic 采用了高度模块化的现代 Python 架构：

```
zoaholic/
├── main.py              # FastAPI 应用入口与生命周期管理
├── core/                # 核心引擎
│   ├── channels/        # 各大厂商 API 适配器注册表 (AWS, Azure, Vertex 等)
│   ├── dialects/        # 方言转换引擎 (OpenAI ↔ Claude ↔ Gemini)
│   ├── plugins/         # 插件生命周期管理与拦截器
│   ├── handler.py       # 模型请求处理核心 (ModelRequestHandler)
│   └── routing.py       # 智能路由与负载均衡
├── routes/              # FastAPI 路由层
├── plugins/             # 官方与用户插件
├── frontend/            # React 前端工程 (Vite + Tailwind)
└── docs/                # 文档
```

## 📝 配置指南

Zoaholic 兼容 uni-api 的 `api.yaml` 配置格式。详细的高级配置选项（如权重负载均衡、Token 限流、Vertex 配置等）请参考原项目文档或配置文件示例。

## 🤝 致谢

- [uni-api](https://github.com/yym68686/uni-api) - 本项目的优秀上游基础

## 🛠️ 开发工具

本项目使用 [Lim Code](https://github.com/Lianues/Lim-Code) 进行开发。

## 📄 许可证

MIT License