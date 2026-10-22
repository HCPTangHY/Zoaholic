# ===============
# Stage 1: Frontend build
# ===============
FROM node:20-slim AS frontend_builder
WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ===============
# Stage 2: Python deps
# ===============
FROM python:3.11 AS builder
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
# 使用 uv export 导出依赖列表，然后安装到系统 Python
# 注意：builder 阶段只拷贝了 pyproject.toml/uv.lock，没有拷贝项目源码。
# uv export 默认会尝试将“当前项目”也写入 requirements（从而触发 setuptools 校验源码/README 是否存在）。
# 这里用 --no-emit-project 仅导出第三方依赖，避免在 CI/Docker 构建时报：
#  - File '/app/README.md' cannot be found
#  - package directory 'core' does not exist
RUN uv export --frozen --no-dev --no-hashes --no-emit-project -o requirements.txt && \
    uv pip install --system --no-cache -r requirements.txt

# ===============
# Stage 3: Runtime
# ===============
FROM python:3.11-slim-bullseye

EXPOSE 8000
WORKDIR /home

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY . .

# 将前端产物放入后端 static/ 目录（FastAPI 直接挂载）
COPY --from=frontend_builder /app/static ./static

# 部分云平台会注入 $PORT；用 shell 形式让变量生效
# 部分云平台会注入 $PORT；用 shell 形式让变量生效
# 修改原因：--forwarded-allow-ips '*' 默认信任任意对端，可被直连伪造来源 IP。
# 修改方式：默认仅信任回环，反代在容器外时通过 FORWARDED_ALLOW_IPS 指定网桥/代理地址；
# 监听地址同样支持 HOST 覆盖（容器内默认 0.0.0.0）。
# 目的：与应用层 TRUSTED_PROXIES 一致，收紧代理信任边界。
CMD ["sh", "-c", "python -m uvicorn main:app --host ${HOST:-0.0.0.0} --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
