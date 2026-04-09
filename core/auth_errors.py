"""
认证错误码与辅助方法。

用于统一后端鉴权/登录相关错误的 message + error_code，
避免前后端仅靠文案耦合。
"""

from typing import Any, Optional

from fastapi import HTTPException


AUTH_API_KEY_INVALID = "AUTH_API_KEY_INVALID"
AUTH_ADMIN_CREDENTIALS_INVALID = "AUTH_ADMIN_CREDENTIALS_INVALID"
AUTH_TOKEN_INVALID = "AUTH_TOKEN_INVALID"
AUTH_PERMISSION_DENIED = "AUTH_PERMISSION_DENIED"
AUTH_LOGIN_INVALID_CREDENTIALS = "AUTH_LOGIN_INVALID_CREDENTIALS"


AUTH_ERROR_DEFINITIONS: dict[str, dict[str, Any]] = {
    AUTH_API_KEY_INVALID: {
        "message": "Invalid or missing API Key",
        "status_code": 403,
    },
    AUTH_ADMIN_CREDENTIALS_INVALID: {
        "message": "Invalid or missing credentials",
        "status_code": 403,
    },
    AUTH_TOKEN_INVALID: {
        "message": "Invalid or expired token",
        "status_code": 403,
    },
    AUTH_PERMISSION_DENIED: {
        "message": "Permission denied",
        "status_code": 403,
    },
    AUTH_LOGIN_INVALID_CREDENTIALS: {
        "message": "Invalid username or password",
        "status_code": 403,
    },
}


LOCAL_AUTH_FAILURE_CODES = frozenset({
    AUTH_API_KEY_INVALID,
    AUTH_ADMIN_CREDENTIALS_INVALID,
    AUTH_TOKEN_INVALID,
    AUTH_PERMISSION_DENIED,
})


def get_auth_error_message(error_code: str) -> str:
    definition = AUTH_ERROR_DEFINITIONS.get(error_code)
    if not definition:
        return "Authentication failed"
    return str(definition["message"])


def get_auth_error_status_code(error_code: str) -> int:
    definition = AUTH_ERROR_DEFINITIONS.get(error_code)
    if not definition:
        return 403
    return int(definition["status_code"])


def build_auth_error_detail(error_code: str, **extra: Any) -> dict[str, Any]:
    detail = {
        "message": get_auth_error_message(error_code),
        "error_code": error_code,
    }
    for key, value in extra.items():
        if value is not None:
            detail[key] = value
    return detail


def auth_http_exception(error_code: str, *, status_code: Optional[int] = None, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code or get_auth_error_status_code(error_code),
        detail=build_auth_error_detail(error_code, **extra),
    )
