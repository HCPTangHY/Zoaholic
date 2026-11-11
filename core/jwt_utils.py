import base64
import hashlib
import hmac
import json
import math
import os
import time
from typing import Any, Dict, Optional


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    s = data.encode("ascii")
    s += b"=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


_CACHED_SECRET: Optional[str] = None
LEGACY_JWT_SECRET = "dev-insecure-jwt-secret"


def set_jwt_secret(secret: str) -> None:
    """设置当前进程使用的 JWT 签名密钥（通常由启动阶段从 DB/文件加载后注入）。"""

    global _CACHED_SECRET
    secret = (secret or "").strip()
    if not secret or secret == LEGACY_JWT_SECRET:
        raise ValueError("A trusted JWT signing secret is required")
    _CACHED_SECRET = secret


def _jwt_secret() -> str:
    """获取 JWT 签名密钥。

    显式环境变量优先，否则使用启动或初始化时加载的持久化密钥。
    未就绪时拒绝签发和校验，不提供默认或进程随机密钥。
    """

    secret = (os.getenv("JWT_SECRET") or "").strip() or _CACHED_SECRET
    if not secret or secret == LEGACY_JWT_SECRET:
        raise RuntimeError("JWT signing secret is not initialized")
    return secret


def issue_jwt(payload: Dict[str, Any], *, expires_in_seconds: int = 7 * 24 * 3600) -> str:
    header = {"alg": "HS256", "typ": "JWT"}

    now = int(time.time())
    full_payload = dict(payload)
    full_payload.setdefault("iat", now)
    full_payload.setdefault("exp", now + int(expires_in_seconds))

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(full_payload, separators=(",", ":")).encode("utf-8"))

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(_jwt_secret().encode("utf-8"), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64url_encode(sig)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def decode_jwt(token: str) -> Optional[Dict[str, Any]]:
    if not token or token.count(".") != 2:
        return None

    try:
        header_b64, payload_b64, sig_b64 = token.split(".", 2)
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        if not isinstance(header, dict) or header.get("alg") != "HS256":
            return None
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected_sig = hmac.new(
            _jwt_secret().encode("utf-8"), signing_input, hashlib.sha256
        ).digest()

        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict):
            return None

        exp = payload.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
            return None
        if time.time() >= exp:
            return None

        return payload
    except Exception:
        return None


def is_admin_jwt(token: str) -> bool:
    payload = decode_jwt(token)
    if not payload:
        return False
    return str(payload.get("role", "")).lower() == "admin"
