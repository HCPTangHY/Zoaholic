"""
API key 池和限流器模块。

修改原因：core.utils.py 文件过大，API key 轮转、限流和运行时禁用状态属于独立职责。
修改方式：将 parse_rate_limit、ThreadSafeCircularList、ApiKeyRateLimitRegistry 和 provider_api_circular_list 移入本文件。
目的：保证 provider_api_circular_list 只在本文件创建一次，并由 core.utils 重新导出同一个对象。
"""

import asyncio
import json
import random
import re
from collections import defaultdict, deque
from time import time

from fastapi import HTTPException

from .log_config import logger


def parse_rate_limit(limit_string):
    # 定义时间单位到秒的映射
    time_units = {
        's': 1, 'sec': 1, 'second': 1,
        'm': 60, 'min': 60, 'minute': 60,
        'h': 3600, 'hr': 3600, 'hour': 3600,
        'd': 86400, 'day': 86400,
        'mo': 2592000, 'month': 2592000,
        'y': 31536000, 'year': 31536000,
        'tpr': -1,
    }

    # 处理多个限制条件
    limits = []
    for limit in limit_string.split(','):
        limit = limit.strip()
        # 使用正则表达式匹配数字和单位
        match = re.match(r'^(\d+)/(\w+)$', limit)
        if not match:
            raise ValueError(f"Invalid rate limit format: {limit}")

        count, unit = match.groups()
        count = int(count)

        # 转换单位到秒
        if unit not in time_units:
            raise ValueError(f"Unknown time unit: {unit}")

        seconds = time_units[unit]
        limits.append((count, seconds))

    return limits


# ==================== 运行时自动禁用 Key 持久化 ====================
import os as _os
import threading as _threading

_RT_DISABLED_FILE = _os.path.join(
    _os.getenv("DATA_DIR", "/home/data"), "runtime_disabled_keys.json"
)
_rt_save_lock = _threading.Lock()


def _save_all_auto_disabled():
    """将所有渠道的运行时自动禁用状态持久化到 JSON 文件。"""
    try:
        snapshot = {}
        for pname, clist in provider_api_circular_list.items():
            if clist.auto_disabled_info:
                entries = {}
                for k, info in clist.auto_disabled_info.items():
                    cooling_val = clist.cooling_until.get(k, 0)
                    entries[k] = {
                        "cooling_until": None if cooling_val == float('inf') else cooling_val,
                        "disabled_at": info.get("disabled_at", 0),
                        "duration": info.get("duration", 0),
                        "reason": info.get("reason", ""),
                    }
                snapshot[pname] = entries
        with _rt_save_lock:
            _os.makedirs(_os.path.dirname(_RT_DISABLED_FILE), exist_ok=True)
            tmp = _RT_DISABLED_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            _os.replace(tmp, _RT_DISABLED_FILE)
    except Exception as e:
        logger.debug(f"[auto_disable_persist] save failed: {e}")


def load_auto_disabled_snapshot() -> dict:
    """从文件加载运行时自动禁用快照。返回 {provider_name: {key: {...}}}"""
    try:
        if _os.path.exists(_RT_DISABLED_FILE):
            with open(_RT_DISABLED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.debug(f"[auto_disable_persist] load failed: {e}")
    return {}


def restore_auto_disabled():
    """启动时从持久化文件恢复所有渠道的自动禁用状态。

    应在 provider_api_circular_list 初始化完成后调用。
    已过期的非永久禁用条目会被跳过。
    """
    snapshot = load_auto_disabled_snapshot()
    if not snapshot:
        return
    now = time()
    restored = 0
    for pname, entries in snapshot.items():
        clist = provider_api_circular_list.get(pname)
        if not clist:
            continue
        for k, info in entries.items():
            if k not in clist.items:
                continue
            cooling = info.get("cooling_until")
            if cooling is None:
                cooling = float('inf')  # 永久禁用
            elif cooling <= now:
                continue  # 已过期，跳过
            clist.cooling_until[k] = cooling
            clist.auto_disabled_info[k] = {
                "disabled_at": info.get("disabled_at", 0),
                "duration": info.get("duration", 0),
                "reason": info.get("reason", ""),
            }
            restored += 1
    if restored:
        logger.info(f"[auto_disable_persist] Restored {restored} disabled key(s) from snapshot")


class ThreadSafeCircularList:
    def __init__(self, items = [], rate_limit={"default": "999999/min"}, schedule_algorithm="round_robin", provider_name=None, disabled_keys=None):
        self.provider_name = provider_name
        self.original_items = list(items)
        self.schedule_algorithm = schedule_algorithm
        # 存储禁用的 key 集合
        self.disabled_keys = set(disabled_keys) if disabled_keys else set()

        if schedule_algorithm == "random":
            self.items = random.sample(items, len(items))
        elif schedule_algorithm == "round_robin":
            self.items = items
        elif schedule_algorithm == "fixed_priority":
            self.items = items
        elif schedule_algorithm == "smart_round_robin":
            self.items = items
        elif schedule_algorithm == "sticky_ip":
            self.items = items
        else:
            self.items = items
            logger.warning(f"Unknown schedule algorithm: {schedule_algorithm}, use (round_robin, random, fixed_priority, smart_round_robin, sticky_ip) instead")
            self.schedule_algorithm = "round_robin"

        self.index = 0
        self.lock = asyncio.Lock()
        # sticky_ip session 表: {client_ip: (key_index, expire_time)}
        self._sticky_sessions: dict[str, tuple[int, float]] = {}
        # 修改原因：最内层 list 会保留所有历史时间戳，并且按 model 自动增长后长期不清理。
        # 修改方式：将 self.requests[api_key][model] 初始化为 deque(maxlen=1000)，并记录 next 调用次数用于惰性清理。
        # 目的：限制单个 model 的时间戳数量，同时给过期 model/api_key 项提供清理入口。
        self.requests = defaultdict(lambda: defaultdict(lambda: deque(maxlen=1000)))
        self._request_cleanup_counter = 0
        self.cooling_until = defaultdict(float)
        self.rate_limits = {}
        self.reordering_task = None
        self.auto_disabled_info = {}  # key -> {"disabled_at": float, "duration": int, "reason": str}

        if isinstance(rate_limit, dict):
            for rate_limit_model, rate_limit_value in rate_limit.items():
                self.rate_limits[rate_limit_model] = parse_rate_limit(rate_limit_value)
        elif isinstance(rate_limit, str):
            self.rate_limits["default"] = parse_rate_limit(rate_limit)
        else:
            logger.error(f"Error ThreadSafeCircularList: Unknown rate_limit type: {type(rate_limit)}, rate_limit: {rate_limit}")

        if self.schedule_algorithm == "smart_round_robin":
            logger.info(f"Initializing '{self.provider_name}' with 'smart_round_robin' algorithm.")
            self._trigger_reorder()

    async def reset_items(self, new_items: list):
        """Safely replaces the current list of items with a new one."""
        async with self.lock:
            if self.items != new_items:
                self.items = new_items
                self.index = 0
                logger.info(f"Provider '{self.provider_name}' API key list has been reset and reordered.")

    def _trigger_reorder(self):
        """Asynchronously triggers the reordering task if not already running."""
        if self.provider_name and (self.reordering_task is None or self.reordering_task.done()):
            logger.info(f"Triggering reorder for provider '{self.provider_name}'...")
            try:
                loop = asyncio.get_running_loop()
                self.reordering_task = loop.create_task(self._reorder_keys())
            except RuntimeError:
                logger.warning(f"No running event loop to trigger reorder for '{self.provider_name}'.")

    async def _reorder_keys(self):
        """Performs the actual reordering logic."""
        # 修改原因：core.key_pool 属于 core 底层单例模块，反向导入根 utils 门面会扩大循环依赖风险。
        # 修改方式：直接从归位后的 core.key_stats 导入排序函数，不再经过 utils.py。
        # 目的：保持全局 key pool 单例稳定，并让依赖方向回到 core 内部业务模块。
        from core.key_stats import get_sorted_api_keys
        try:
            sorted_keys = await get_sorted_api_keys(self.provider_name, self.original_items, group_size=100)
            if sorted_keys:
                await self.reset_items(sorted_keys)
        except Exception as e:
            logger.error(f"Error during key reordering for provider '{self.provider_name}': {e}")

    async def set_cooling(self, item: str, cooling_time: int = 60):
        """设置某个 item 进入冷却状态

        Args:
            item: 需要冷却的 item
            cooling_time: 冷却时间(秒)，默认60秒
        """
        if item is None:
            return
        now = time()
        async with self.lock:
            self.cooling_until[item] = now + cooling_time
            # 修改原因：requests 的值已经是按 model 分组的 deque，不能再按旧 list 结构直接替换。
            # 修改方式：冷却时只设置 cooling_until，不主动改写 requests 结构，交给窗口清理和过期清理处理。
            # 目的：避免把 requests[item] 误改成 list，保持嵌套 deque 结构一致。
            logger.warning(f"API key {item} 已进入冷却状态，冷却时间 {cooling_time} 秒")

    async def set_auto_disabled(self, item: str, duration: int = 0, reason: str = ""):
        """自动禁用某个 Key。

        通过设置 cooling_until 实现，复用现有的 is_rate_limited 判断链路。
        duration=0 表示永久禁用（直到手动恢复或进程重启）。
        duration>0 表示禁用指定秒数后自动恢复。

        Args:
            item: API key
            duration: 禁用时长（秒），0 表示永久
            reason: 禁用原因（用于日志和 API 展示）
        """
        if item is None:
            return
        now = time()
        async with self.lock:
            if duration > 0:
                self.cooling_until[item] = now + duration
            else:
                self.cooling_until[item] = float('inf')
            self.auto_disabled_info[item] = {
                "disabled_at": now,
                "duration": duration,
                "reason": reason,
            }
        logger.warning(
            f"[auto_disable] Key {item} disabled for provider {self.provider_name}, "
            f"duration={'permanent' if duration == 0 else f'{duration}s'}, reason: {reason}"
        )
        _save_all_auto_disabled()

    async def clear_auto_disabled(self, item: str):
        """手动恢复一个被自动禁用的 Key，清除冷却和元数据。"""
        async with self.lock:
            self.cooling_until[item] = 0.0
            self.auto_disabled_info.pop(item, None)
        _save_all_auto_disabled()

    async def get_auto_disabled_keys(self) -> list:
        """返回当前被自动禁用的 Key 列表及其剩余时间。

        同时清理已自然过期的记录。
        """
        now = time()
        async with self.lock:
            expired = [k for k in self.auto_disabled_info if now >= self.cooling_until.get(k, 0)]
            for k in expired:
                self.auto_disabled_info.pop(k, None)
            result = []
            for item, info in self.auto_disabled_info.items():
                until = self.cooling_until.get(item, 0)
                remaining = -1 if until == float('inf') else max(0, int(until - now))
                result.append({"key": item, "remaining_seconds": remaining, "duration": info.get("duration", 0), "reason": info.get("reason", "")})
            return result

    def is_key_disabled(self, item: str) -> bool:
        """检查某个 key 是否被禁用
        
        Args:
            item: API key
            
        Returns:
            bool: 如果 key 被禁用返回 True，否则返回 False
        """
        return item in self.disabled_keys
    
    def set_key_disabled(self, item: str, disabled: bool = True):
        """设置某个 key 的禁用状态
        
        Args:
            item: API key
            disabled: True 表示禁用，False 表示启用
        """
        if disabled:
            self.disabled_keys.add(item)
        else:
            self.disabled_keys.discard(item)
    
    def update_disabled_keys(self, disabled_keys: set):
        """更新禁用的 key 集合
        
        Args:
            disabled_keys: 新的禁用 key 集合
        """
        self.disabled_keys = set(disabled_keys) if disabled_keys else set()

    def _cleanup_stale_request_keys(self):
        """清理超过 1 小时没有新请求的 requests 索引。

        调用方应在持有 self.lock 时执行本方法，避免清理期间与请求记录写入交错。
        """
        now = time()
        stale_before = now - 3600
        # 修改原因：即使单个 deque 有 maxlen，不再使用的 model key 仍会留在嵌套字典中。
        # 修改方式：删除空时间戳队列，以及最后一条时间戳早于 1 小时前的 model；再删除空 api_key 项。
        # 目的：释放长期不再访问的 model/api_key 索引，避免 requests 字典无限增长。
        for api_key, model_requests in list(self.requests.items()):
            for model_key, timestamps in list(model_requests.items()):
                if not timestamps or timestamps[-1] <= stale_before:
                    del model_requests[model_key]
            if not model_requests:
                del self.requests[api_key]

    async def is_rate_limited(self, item, model: str = None, is_check: bool = False) -> bool:
        now = time()
        # 检查是否被禁用
        if self.is_key_disabled(item):
            return True
        # 检查是否在冷却中
        if now < self.cooling_until.get(item, 0):
            return True

        # 获取适用的速率限制

        if model:
            model_key = model
        else:
            model_key = "default"

        rate_limit = None
        matched_default = False
        # 先尝试精确匹配
        if model and model in self.rate_limits:
            rate_limit = self.rate_limits[model]
        else:
            # 如果没有精确匹配，尝试模糊匹配
            for limit_model in self.rate_limits:
                if limit_model != "default" and model and limit_model in model:
                    rate_limit = self.rate_limits[limit_model]
                    break

        # 如果都没匹配到，使用默认值
        if rate_limit is None:
            rate_limit = self.rate_limits.get("default", [(999999, 60)])  #默认限制
            matched_default = True

        # 检查所有速率限制条件
        for limit_count, limit_period in rate_limit:
            if matched_default:
                # default 规则：跨所有模型汇总计数，作为该 key 的总量限制
                recent_requests = sum(
                    1 for mk_reqs in self.requests[item].values()
                    for req in mk_reqs if req > now - limit_period
                )
            else:
                # 模型特定规则：仅计算该模型的请求数
                recent_requests = sum(1 for req in self.requests[item][model_key] if req > now - limit_period)
            if recent_requests >= limit_count:
                if not is_check:
                    logger.warning(f"API key {item}: model: {model_key} has been rate limited ({limit_count}/{limit_period} seconds)")
                return True

        # 清理太旧的请求记录
        max_period = max(period for _, period in rate_limit)
        model_requests = self.requests[item][model_key]
        cutoff = now - max_period
        # 修改原因：requests 最内层已改为 deque，若用列表推导重新赋值会丢失 maxlen 上限。
        # 修改方式：按时间顺序从左侧弹出超出限流窗口的时间戳，保留同一个 deque 对象。
        # 目的：继续清理窗口外记录，同时保持 deque(maxlen=1000) 的内存上限。
        while model_requests and model_requests[0] <= cutoff:
            model_requests.popleft()

        # 记录新的请求
        if not is_check:
            self.requests[item][model_key].append(now)

        return False


    async def next(self, model: str = None):
        async with self.lock:
            self._request_cleanup_counter += 1
            # 修改原因：requests 的嵌套 model key 只靠 maxlen 不能释放不再使用的字典项。
            # 修改方式：在已有锁内每 100 次 next 调用执行一次轻量过期清理。
            # 目的：保证清理操作线程安全，并把额外开销分摊到请求路径中。
            if self._request_cleanup_counter % 100 == 0:
                self._cleanup_stale_request_keys()

            client_ip = ""  # sticky_ip 用

            if self.schedule_algorithm == "fixed_priority":
                self.index = 0

            if self.schedule_algorithm == "sticky_ip" and len(self.items) > 0:
                from .middleware import request_info
                info = request_info.get()
                client_ip = info.get("client_ip", "") if isinstance(info, dict) else ""
                now = time()
                session = self._sticky_sessions.get(client_ip)
                if session and session[1] > now and session[0] < len(self.items):
                    # 已有 session 且未过期且 index 合法 → 粘滞
                    self.index = session[0]
                else:
                    # 新 IP 或 session 过期 → round_robin 游标分配，保证均匀
                    # self.index 已经是当前 round_robin 位置，不用动
                    pass
                # 不管是粘滞还是新分配，都在拿到 key 后更新 session（见下方）

            # 检查是否即将完成一个循环，并据此触发重排序
            if self.schedule_algorithm == "smart_round_robin" and len(self.items) > 0 and self.index == len(self.items) - 1:
                self._trigger_reorder()

            start_index = self.index
            while True:
                item = self.items[self.index]
                self.index = (self.index + 1) % len(self.items)

                if not await self.is_rate_limited(item, model):
                    # sticky_ip: 记录本次分配，1小时 TTL
                    if self.schedule_algorithm == "sticky_ip" and client_ip:
                        key_idx = (self.index - 1) % len(self.items)
                        self._sticky_sessions[client_ip] = (key_idx, time() + 3600)
                        # 惰性清理过期 session（每 100 次）
                        if len(self._sticky_sessions) > 100 and len(self._sticky_sessions) % 50 == 0:
                            expired = [k for k, (_, exp) in self._sticky_sessions.items() if exp <= time()]
                            for k in expired:
                                self._sticky_sessions.pop(k, None)
                    return item

                # 修改原因：遍历全部 key 时不释放事件循环会在高并发下造成级联阻塞 30 秒以上，
                # 触发 watchdog 写 thread dump 后 PM2 重启。
                # 修改方式：每次限流 key 判定后显式让出事件循环。
                # 目的：避免持锁遍历导致的级联阻塞和进程重启。
                await asyncio.sleep(0)

                # 如果已经检查了所有的 API key 都被限制
                if self.index == start_index:
                    logger.warning("All API keys are rate limited!")
                    raise HTTPException(status_code=429, detail="Too many requests")

    async def is_tpr_exceeded(self, model: str = None, tokens: int = 0) -> bool:
        """Checks if the request exceeds the TPR (Tokens Per Request) limit."""
        if not tokens:
            return False

        async with self.lock:
            rate_limit = None
            model_key = model or "default"
            if model and model_key in self.rate_limits:
                rate_limit = self.rate_limits[model_key]
            else:
                # fuzzy match
                for limit_model in self.rate_limits:
                    if limit_model != "default" and model and limit_model in model:
                        rate_limit = self.rate_limits[limit_model]
                        break
            if rate_limit is None:
                rate_limit = self.rate_limits.get("default", [])

            for limit_count, limit_period in rate_limit:
                if limit_period == -1:  # TPR limit
                    if tokens > limit_count:
                        # logger.warning(f"API provider for model {model_key} exceeds TPR limit ({tokens}/{limit_count}).")
                        return True
        return False

    async def is_all_rate_limited(self, model: str = None) -> bool:
        """检查是否所有的items都被速率限制

        与next方法不同，此方法不会改变任何内部状态（如self.index），
        仅返回一个布尔值表示是否所有的key都被限制。

        Args:
            model: 要检查的模型名称，默认为None

        Returns:
            bool: 如果所有items都被速率限制返回True，否则返回False
        """
        if len(self.items) == 0:
            return False

        # 快照 items，避免长时间持锁导致事件循环阻塞
        async with self.lock:
            items_snapshot = list(self.items)

        for item in items_snapshot:
            # 跳过禁用的 key
            if self.is_key_disabled(item):
                continue
            if not await self.is_rate_limited(item, model, is_check=True):
                return False
            # 修改原因：is_all_rate_limited 同样遍历全部 key 调用 is_rate_limited，
            # 事件循环在此过程中不释放，与 next() 产生相同的阻塞问题。
            # 修改方式：每个 key 检查后让出事件循环。
            # 目的：与 next() 的修复保持一致，避免级联阻塞。
            await asyncio.sleep(0)

        # 如果遍历完所有items都被限制，返回True
        return True
    
    def get_enabled_items_count(self) -> int:
        """返回启用的项目数量。

        排除配置禁用和运行时自动禁用（未过期）的 Key。
        对 auto_disabled_info 中已过期的记录不计入禁用。

        Returns:
            int: 启用的 items 数量
        """
        now = time()
        return len([item for item in self.items
                    if not self.is_key_disabled(item) and not (
                        item in self.auto_disabled_info and now < self.cooling_until.get(item, 0)
                    )])

    async def after_next_current(self):
        # 返回当前取出的 API，因为已经调用了 next，所以当前API应该是上一个
        if len(self.items) == 0:
            return None
        async with self.lock:
            item = self.items[(self.index - 1) % len(self.items)]
            return item

    def get_items_count(self) -> int:
        """返回列表中的项目数量

        Returns:
            int: items列表的长度
        """
        return len(self.items)

def circular_list_encoder(obj):
    if isinstance(obj, ThreadSafeCircularList):
        return obj.to_dict()
    raise TypeError(f'Object of type {obj.__class__.__name__} is not JSON serializable')

# 修改原因：defaultdict 会在读取不存在 provider 时自动创建空 ThreadSafeCircularList，造成长期驻留对象。
# 修改方式：改为普通 dict，配置加载仍用 provider_api_circular_list[key] = value 显式写入。
# 目的：避免读路径隐式分配 key 池，降低 provider 名拼写错误或缺失配置带来的内存增长。
provider_api_circular_list = {}


class ApiKeyRateLimitRegistry(dict):
    """
    API Key 限流器注册表
    
    按需自动创建限流器，解决动态添加 API key 时没有对应限流器的问题。
    继承 dict 并重写 __missing__，在访问不存在的 key 时自动创建正确配置的限流器。
    """
    
    def __init__(self, config_getter, api_list_getter):
        """
        Args:
            config_getter: 获取当前配置的函数，返回 app.state.config
            api_list_getter: 获取当前 API 列表的函数，返回 app.state.api_list
        """
        super().__init__()
        self._config_getter = config_getter
        self._api_list_getter = api_list_getter
    
    def __missing__(self, api_key: str):
        """
        当访问不存在的 key 时自动创建限流器
        """
        # 修改原因：core.key_pool 会被 core.utils 顶层导入，顶层导入 safe_get 会造成循环导入。
        # 修改方式：只在动态创建限流器时延迟导入 core.utils.safe_get。
        # 目的：保持原有配置读取逻辑，同时保证拆分后的模块导入链稳定。
        from core.utils import safe_get

        config = self._config_getter()
        api_list = self._api_list_getter()
        
        # 查找 API key 的配置
        try:
            api_index = api_list.index(api_key)
            rate_limit = safe_get(
                config, 'api_keys', api_index, "preferences", "rate_limit",
                default={"default": "999999/min"}
            )
        except (ValueError, IndexError):
            # 找不到配置，使用默认限流
            rate_limit = {"default": "999999/min"}
        
        # 创建限流器并缓存
        limiter = ThreadSafeCircularList(
            [api_key],
            rate_limit,
            "round_robin"
        )
        self[api_key] = limiter
        return limiter
