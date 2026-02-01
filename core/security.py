import base64
import hashlib
import hmac
import os


# 说明：
# - 不引入额外依赖（如 passlib/bcrypt），使用标准库 PBKDF2-HMAC。
# - 存储格式：pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>

_DEFAULT_ITERATIONS = 120_000


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    if password is None:
        raise ValueError("password is required")
    password_bytes = password.encode("utf-8")

    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, iterations)

    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(dk).decode("ascii")
    return f"pbkdf2_sha256${iterations}${salt_b64}${dk_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash or not isinstance(password_hash, str):
        return False

    try:
        scheme, iterations_s, salt_b64, dk_b64 = password_hash.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64.encode("ascii"))
        expected = base64.b64decode(dk_b64.encode("ascii"))
    except Exception:
        return False

    password_bytes = (password or "").encode("utf-8")
    actual = hashlib.pbkdf2_hmac("sha256", password_bytes, salt, iterations)

    return hmac.compare_digest(actual, expected)
