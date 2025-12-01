FROM python:3.11 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
# 使用 uv export 导出依赖列表，然后安装到系统 Python
RUN uv export --frozen --no-dev --no-hashes -o requirements.txt && \
    uv pip install --system --no-cache -r requirements.txt

FROM python:3.11-slim-bullseye
EXPOSE 8000
WORKDIR /home
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY . .
ENTRYPOINT ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]