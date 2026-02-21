# Zoaholic

<p align="center">
  <img src="frontend/public/zoaholic.png" alt="Zoaholic Logo" width="200"/>
</p>

<p align="center">
  <strong>Multi‑dialect (OpenAI / Claude / Gemini) LLM API Gateway + Web Admin Console</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> | <a href="./README_EN.md">English</a>
</p>

## What is this?

Zoaholic is a self-hostable LLM API gateway based on the excellent open-source project [uni-api](https://github.com/yym68686/uni-api).

As the LLM ecosystem evolves, OpenAI is no longer the only “standard”. Zoaholic introduces a **Multi‑Dialect architecture** that natively understands and translates between:

- OpenAI (`/v1/chat/completions`)
- Anthropic Claude (`/v1/messages`)
- Google Gemini (`/v1beta/...`)

Zoaholic provides:

- Multi-dialect translation: bidirectional protocol conversion, unified routing, SSE streaming
- Provider & key management: configure `providers` / `api_keys` in the web console
- Stats & logs: request stats and log viewer (optional DB)
- Plugin system: interceptor-based request/response extensions

## Key Features

### Multi‑Dialect Gateway

Zoaholic does NOT force everything into the OpenAI format. It supports protocol-level translation and streaming:

- `/v1/chat/completions` (OpenAI) → route to Claude/Gemini backends
- `/v1/messages` (Claude) → route to OpenAI/Gemini backends
- `/v1beta/models/...` (Gemini) → same idea

### Dynamic Plugin System

Python hot-pluggable plugins (interceptors) that extend gateway behavior without modifying core code.

Built-in featured plugins:

- `claude_thinking`: converts Claude `-thinking` requests into reasoning streams and splits `reasoning_content`/`content`
- `gemini_empty_retry`: retries when Gemini occasionally returns empty responses
- `claude_tools`: enhances Claude function/tool calling

### Enterprise-grade Load Balancing

Inherited from uni-api routing core (`core/routing.py`):

- Scheduling: fixed priority, round-robin, weighted, lottery, smart routing
- HA: auto retry, cooldown, per-model timeout
- Rate limit & concurrency: based on `ThreadSafeCircularList`

---

## Quick Start (Recommended: Docker + Setup Wizard)

### 1) Prepare a database (PostgreSQL / Cloudflare D1 recommended for cloud)

Platforms like Render/Aiven/Railway usually provide a `DATABASE_URL`.

If you deploy on Cloudflare Workers, you can also use D1 directly:

- `DB_TYPE=d1`
- `D1_ACCOUNT_ID` (or `CF_ACCOUNT_ID`)
- `D1_DATABASE_ID`
- `D1_API_TOKEN` (or `CF_API_TOKEN`, with D1 Query permission)

### 2) Start the service

Example with GHCR image (replace with your own image if you build it yourself):

```bash
docker run --rm -p 8000:8000 \
  -e PORT=8000 \
  -e DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require" \
  ghcr.io/qianzhuowo/zoaholic:latest
```

### 3) First-time initialization

Open:

- `http://localhost:8000/setup`

Follow the wizard to set **admin username/password**.
After initialization, go to `/login` and sign in.

Then configure in the console:

- `providers`: upstream providers (OpenAI/Claude/Gemini/Azure/Vertex/Bedrock…)
- `api_keys`: gateway access keys for your clients

Note:

- Gateway APIs under `/v1/*` areOpenAI-compatible APIs and still use **API Key** authentication.
- Admin console uses **username/password + JWT**.

---

## Cloud Deploy (Render etc.): Which env vars should I set?

Below are the most common and most error-prone environment variables for cloud deployments.

### Required (strongly recommended)

| Variable | Example | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql://...` / `postgres://...` / `mysql://...` / `mysql+asyncmy://...` | Database URL (PostgreSQL or TiDB/MySQL; choose one of `DATABASE_URL` or Cloudflare D1). Stats/logs + config persistence depend on DB. |

> TiDB Cloud (Serverless) usually **requires TLS**. If your URL doesn't include any SSL params, append `?ssl=true` or `?ssl_mode=VERIFY_IDENTITY`.

Render usually injects `PORT` automatically; Zoaholic will read `PORT` as the listening port.

### Recommended

| Variable | Default | Notes |
|---|---:|---|
| `CONFIG_STORAGE` | `file` | Config source strategy: `auto\|db\|file\|url`. Default is `file` (`api.yaml` is the source of truth); for cloud you may use `auto` (file-first + sync to DB) or `db` (DB-first). |
| `SYNC_CONFIG_TO_FILE` | `false` | Whether to write config back to `api.yaml`. Cloud file systems are often ephemeral/readonly, keep `false`. |
| `JWT_SECRET` | (optional) | JWT signing key for admin console. **You can skip it**: on first `/setup`, Zoaholic auto-generates and persists `admin_user.jwt_secret` in DB and reuses it after restarts. For better security, set it explicitly. |
| `DISABLE_DATABASE` | `false` | Disable DB entirely. Cloud usually should NOT disable it (otherwise no config persistence / no stats). |

### Cloudflare D1 (optional)

| Variable | Default | Notes |
|---|---:|---|
| `DB_TYPE` | `sqlite` | Set to `d1` to enable Cloudflare D1 HTTP mode. |
| `D1_ACCOUNT_ID` / `CF_ACCOUNT_ID` | - | Cloudflare Account ID. |
| `D1_DATABASE_ID` | - | D1 Database ID. |
| `D1_API_TOKEN` / `CF_API_TOKEN` | - | Cloudflare API Token with D1 Query permission. |
| `D1_API_BASE_URL` | `https://api.cloudflare.com/client/v4` | D1 API base URL (usually unchanged). |
| `D1_TIMEOUT_SECONDS` | `30` | D1 HTTP request timeout in seconds. |

### Optional (advanced)

| Variable | Example | Notes |
|---|---|---|
| `CONFIG_YAML` | raw YAML | Provide seed config via env var.
| `CONFIG_YAML_BASE64` | base64(YAML) | Recommended for multiline YAML. Loaded once and persisted into DB.
| `CONFIG_URL` | `https://.../api.yaml` | Fetch seed config from URL and persist into DB.
| `ADMIN_API_KEY` / `ADMIN_API_KEYS` | `sk-...` | If no config sources exist, create a minimal boot config with only admin key(s).
| `DEBUG` | `true/false` | Enable debug logs.

---

## Config Persistence (Store config in DB)

Zoaholic can persist the runtime config (previously `api.yaml`) into the database.

Default behavior (`CONFIG_STORAGE=file`):

- Load from `api.yaml` (`api.yaml` is the source of truth)
- Saving config in the admin UI will write back to `api.yaml` (requires writable filesystem / volume mount)

Optional cloud mode (`CONFIG_STORAGE=auto`, file-first + sync to DB):

- Load from `api.yaml` / `CONFIG_YAML(_BASE64)` / `CONFIG_URL`
- Then persist into DB for backup / multi-instance sharing (won't override `api.yaml`)

Optional DB-first mode (`CONFIG_STORAGE=db`):

- If DB has config: load from DB
- If DB has no config yet: load once from `CONFIG_YAML(_BASE64)` / `CONFIG_URL` / `api.yaml` as a seed, then persist into DB

Once persisted:

- `app_config.config_json` is the primary storage (PostgreSQL uses JSONB)
- `app_config.config_yaml` is optional (for export/debugging)

---

## Local Development (without Docker)

```bash
# Backend
python -m venv .venv
# Activate venv then install dependencies
pip install -r requirements.txt

# Frontend (build outputs to ../static)
cd frontend
npm install
npm run build
cd ..

python main.py
```

---

## FAQ

### 1) Why do I get 403 for /v1 endpoints?

`/v1/*` endpoints are gateway APIs and require an API key by default:

- `Authorization: Bearer sk-...`
- or `x-api-key: sk-...`

Please configure `api_keys` in the admin console first.

### 2) Can I skip JWT_SECRET?

Yes. On first `/setup`, Zoaholic will auto-generate and persist a `jwt_secret` into the DB and reuse it on restarts.
For security/compliance, it is still recommended to explicitly set `JWT_SECRET` in your cloud environment.

---

## Credits

- Upstream project: [uni-api](https://github.com/yym68686/uni-api)

## License

MIT
