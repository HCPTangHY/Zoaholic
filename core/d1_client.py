import json
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import httpx


def format_d1_datetime(value: datetime) -> str:
    """将 datetime 统一格式化为 D1/SQLite 友好的 UTC 字符串。"""

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def parse_d1_datetime(value: Any) -> Optional[datetime]:
    """将 D1 返回的时间值解析为带 UTC 时区的 datetime。"""

    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None

        # 先尝试 Unix 时间戳字符串
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError:
            pass

        # 兼容 ISO-8601（含 Z）
        iso_text = text
        if iso_text.endswith("Z"):
            iso_text = iso_text[:-1] + "+00:00"

        try:
            dt = datetime.fromisoformat(iso_text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

        # 兼容 SQLite 常见格式
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue

    return None


def normalize_d1_param(value: Any) -> Any:
    """将 Python 参数转换为 D1 HTTP API 友好的参数。"""

    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, datetime):
        return format_d1_datetime(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def normalize_d1_params(params: Optional[Iterable[Any]]) -> list[Any]:
    if params is None:
        return []
    return [normalize_d1_param(v) for v in params]


class D1HTTPClient:
    """Cloudflare D1 HTTP API 轻量客户端。"""

    def __init__(
        self,
        *,
        account_id: str,
        database_id: str,
        api_token: str,
        api_base_url: str = "https://api.cloudflare.com/client/v4",
        timeout_seconds: float = 30.0,
    ):
        self.account_id = account_id.strip()
        self.database_id = database_id.strip()
        self.api_token = api_token.strip()
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

        if not self.account_id:
            raise ValueError("D1 account_id is required")
        if not self.database_id:
            raise ValueError("D1 database_id is required")
        if not self.api_token:
            raise ValueError("D1 api_token is required")

    @property
    def _query_url(self) -> str:
        return (
            f"{self.api_base_url}/accounts/{self.account_id}"
            f"/d1/database/{self.database_id}/query"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _extract_error_message(payload: dict) -> str:
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            messages: list[str] = []
            for item in errors:
                if isinstance(item, dict):
                    msg = item.get("message") or item.get("error")
                    code = item.get("code")
                    if msg and code is not None:
                        messages.append(f"[{code}] {msg}")
                    elif msg:
                        messages.append(str(msg))
                elif isinstance(item, str):
                    messages.append(item)
            if messages:
                return "; ".join(messages)

        result = payload.get("result")
        if isinstance(result, list):
            for block in result:
                if isinstance(block, dict) and block.get("success") is False:
                    msg = block.get("error")
                    if msg:
                        return str(msg)

        return "Unknown D1 API error"

    async def _post_query(self, body: dict) -> dict:
        timeout = httpx.Timeout(self.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(self._query_url, headers=self._headers, json=body)

        try:
            payload = response.json()
        except Exception as exc:
            response.raise_for_status()
            raise RuntimeError(f"Invalid D1 response: {exc}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Invalid D1 response payload")

        if response.status_code >= 400 or payload.get("success") is False:
            raise RuntimeError(self._extract_error_message(payload))

        return payload

    async def query(self, sql: str, params: Optional[Iterable[Any]] = None) -> dict:
        body: dict[str, Any] = {"sql": sql}
        normalized_params = normalize_d1_params(params)
        if normalized_params:
            body["params"] = normalized_params

        payload = await self._post_query(body)
        result = payload.get("result")

        if isinstance(result, list):
            if not result:
                return {"success": True, "results": [], "meta": {}}
            block = result[0]
            if isinstance(block, dict):
                if block.get("success") is False:
                    raise RuntimeError(str(block.get("error") or self._extract_error_message(payload)))
                return block

        if isinstance(result, dict):
            if result.get("success") is False:
                raise RuntimeError(str(result.get("error") or self._extract_error_message(payload)))
            return result

        return {"success": True, "results": [], "meta": {}}

    async def query_all(self, sql: str, params: Optional[Iterable[Any]] = None) -> list[dict]:
        block = await self.query(sql, params)
        rows = block.get("results")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
        return []

    async def query_one(self, sql: str, params: Optional[Iterable[Any]] = None) -> Optional[dict]:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_value(
        self,
        sql: str,
        params: Optional[Iterable[Any]] = None,
        *,
        column: Optional[str] = None,
        default: Any = None,
    ) -> Any:
        row = await self.query_one(sql, params)
        if row is None:
            return default

        if column is not None:
            return row.get(column, default)

        for value in row.values():
            return value
        return default

    async def execute(self, sql: str, params: Optional[Iterable[Any]] = None) -> dict:
        return await self.query(sql, params)
