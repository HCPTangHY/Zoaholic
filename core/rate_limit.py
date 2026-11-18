"""内存限流工具。"""


# 迁移说明：
# 修改原因：该模块承载业务逻辑，不应继续放在 utils_pkg 这种通用工具包中。
# 修改方式：按照 Scout 的归位方案迁移到 core 对应业务模块，并只调整必要的内部导入路径。
# 目的：让业务代码按领域归属维护，同时保留根 utils.py 和 utils_pkg shim 的旧导入兼容性。
import asyncio
from collections import defaultdict
from time import time


class InMemoryRateLimiter:
    MAX_KEYS = 4096

    def __init__(self):
        self.requests = defaultdict(list)
        self._lock = asyncio.Lock()

    async def is_rate_limited(self, key: str, limits) -> bool:
        async with self._lock:
            now = time()
            max_period = max(period for _, period in limits)
            bucket = [req for req in self.requests.get(key, ()) if req > now - max_period]

            for limit, period in limits:
                cutoff = now - period
                if sum(1 for req in bucket if req > cutoff) >= limit:
                    if bucket:
                        self.requests[key] = bucket
                    else:
                        self.requests.pop(key, None)
                    return True

            bucket.append(now)
            self.requests[key] = bucket

            if len(self.requests) > self.MAX_KEYS:
                expired_keys = [k for k, v in self.requests.items() if not v or v[-1] <= now - max_period]
                for k in expired_keys:
                    self.requests.pop(k, None)
                while len(self.requests) > self.MAX_KEYS:
                    oldest_key = next(iter(self.requests))
                    self.requests.pop(oldest_key, None)

            return False
