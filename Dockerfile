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

# Render 会注入 $PORT；用 shell 形式让变量生效
CMD ["sh", "-c", "python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
