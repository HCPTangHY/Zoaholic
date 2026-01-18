# Zoaholic

<p align="center">
  <img src="frontend/public/zoaholic.png" alt="Zoaholic Logo" width="200"/>
</p>

Zoaholic is a next-generation LLM API gateway built on top of the excellent open‑source project uni-api.

While the original uni-api forces all traffic into the OpenAI format, Zoaholic introduces a **Multi-Dialect Architecture**. It natively understands and translates between the OpenAI (`/v1/chat/completions`), Anthropic Claude (`/v1/messages`), and Google Gemini (`/v1beta/...`) protocols.

Combined with a new dynamic Python plugin system and a modern React frontend, Zoaholic is designed for self‑hosted, power‑user scenarios where flexibility and protocol compatibility are paramount.

## Features

### 🗣️ Multi-Dialect Gateway
Send requests in your preferred format, and Zoaholic will automatically translate the prompt format, tool calls, and streaming responses (SSE) to match the upstream provider. 
- Example: Send a Claude API request to an OpenAI GPT-4o backend, and receive a Claude-formatted response.

### 🔌 Dynamic Plugin System
Extend Zoaholic's capabilities without touching the core codebase via Python interceptors.
- **Claude Thinking Plugin**: Automatically injects `<thinking>` pre-fills for models ending in `-thinking`, adjusts max tokens, and elegantly splits the streaming response into `reasoning_content` and standard `content`.
- Add new channels, dialects, and safety filters on the fly.

### 🖥️ Modern React Console
A built-in Material Design UI powered by Vite, React, Tailwind CSS, and Radix UI. Manage channels, test models, and monitor API traffic locally at `http://localhost:8000/`.

### ⚖️ Enterprise-grade Load Balancing
Inherits the robust routing core from uni-api:
- Algorithms: Fixed priority, Round-robin, Weighted, Lottery, and Smart routing.
- High Availability: Automatic retries, channel cooldowns, and independent model timeout handling.
- Fine-grained per-API-key rate limiting.

## Quick Start

Zoaholic uses a single `api.yaml` for configuration, remaining 100% compatible with existing uni-api configs.

A minimal example `api.yaml`:

```yaml
providers:
  - provider: openai
    base_url: https://api.openai.com/v1/chat/completions
    api: sk-your-openai-key

api_keys:
  - api: sk-your-zoaholic-client-key
    model:
      - gpt-4o
```

Run with Docker:

```bash
docker run -d \
  --name zoaholic \
  -p 8000:8000 \
  -v ./api.yaml:/home/api.yaml \
  zoaholic:latest
```

Access the UI at `http://localhost:8000/`.

## Architecture Overview

- `core/dialects/`: The core transformation engine handling request/response translation between API protocols.
- `core/channels/`: The registry for upstream provider adapters (AWS, Azure, Vertex, Cloudflare, etc.).
- `core/plugins/` & `plugins/`: The interceptor-based plugin engine.
- `frontend/`: Standalone React application that mounts statically via FastAPI.

## Relationship to uni-api

Zoaholic is a downstream project of uni-api. The core routing logic (`core/routing.py`) and handler architecture come directly from uni-api.

If you need the original upstream project, please visit:
- GitHub: https://github.com/yym68686/uni-api

Zoaholic builds upon this solid foundation to add Multi-dialect routing, a Plugin engine, and a React GUI.

## Development Tools

This project is developed using [Lim Code](https://github.com/Lianues/Lim-Code).