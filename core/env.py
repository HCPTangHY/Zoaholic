import os
from typing import Final

_TRUE_VALUES: Final[set[str]] = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES: Final[set[str]] = {"0", "false", "no", "n", "off"}


def env_bool(name: str, default: bool = False) -> bool:
    """读取布尔类型环境变量。

    兼容常见写法：1/0, true/false, yes/no, on/off。
    """

    raw = os.getenv(name)
    if raw is None:
        return default

    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    return default
