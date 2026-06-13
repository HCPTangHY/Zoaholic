"""
请求处理入口模块。

修改原因：core.handler.py 原来同时承载普通请求、透传请求和调度处理器，文件体积过大。
修改方式：将普通请求处理移入 core.process_request，将透传请求处理移入 core.passthrough，并在此处重新导出旧名字。
目的：不改变外部调用方式，继续兼容 from core.handler import process_request 等旧导入路径。
"""

# 修改原因：外部代码仍从 core.handler 导入这些请求处理函数。
# 修改方式：在 handler 顶部重新导出拆分后的实现。
# 目的：保持兼容，不要求调用方修改导入路径。
from core.process_request import process_request
from core.passthrough import (
    _filter_passthrough_headers,
    _fetch_passthrough_stream,
    _fetch_passthrough_response,
    _passthrough_error_wrapper,
    process_request_passthrough,
)

import json
import asyncio
from collections import OrderedDict, deque
from core.json_utils import json_dumps_text, json_loads
from datetime import datetime, timedelta, timezone
from time import time
from urllib.parse import urlparse
from typing import Dict, Union, Optional, Any, Callable, List, TYPE_CHECKING

import httpx
from fastapi import HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from starlette.responses import Response

from core.log_config import logger
from core.streaming import LoggingStreamingResponse
from core.request import get_payload
from core.response import fetch_response, fetch_response_stream, check_response
from core.stats import batch_update_channel_stats, update_channel_stats, enqueue_stats
from core.models import (
    RequestModel,
    ImageGenerationRequest,
    AudioTranscriptionRequest,
    ModerationRequest,
    EmbeddingRequest,
)
from core.utils import get_engine, provider_api_circular_list, truncate_for_logging, is_local_api_key
from core.routing import get_right_order_providers
from core.byok import get_byok_real_key, get_byok_template_key, is_byok_provider
from core.error_response import openai_error_response
from utils import safe_get, error_handling_wrapper, apply_custom_headers, has_header_case_insensitive

if TYPE_CHECKING:
    from fastapi import FastAPI

# 默认超时时间（10分钟，支持长时间 reasoning 请求）
# 默认超时时间（10分钟，支持长时间 reasoning 请求）
DEFAULT_TIMEOUT = 600

# 调试模式标志
is_debug = False

# 修改原因：每个请求结束时单独 create_task 写 channel_stats，会在 SQLite 锁等待时堆积大量协程。
# 修改方式：使用固定上限 deque 保存轻量统计参数，并由一个常驻 consumer 批量写入。
# 目的：限制内存占用，避免后台统计写入协程随请求数量线性增长。
_channel_stats_buffer: deque = deque(maxlen=10000)
_channel_stats_consumer_started = False
_channel_stats_flush_event: Optional[asyncio.Event] = None
_CHANNEL_STATS_BATCH_SIZE = 50
_CHANNEL_STATS_FLUSH_INTERVAL = 2


def set_debug_mode(debug: bool):
    """设置调试模式"""
    global is_debug
    is_debug = debug


# 修改原因：provider 轮转游标和锁表原来使用 defaultdict，会随着请求模型和虚拟优先级 key 无上限增长。
# 修改方式：基于 OrderedDict 实现一个轻量 LRU 字典，访问时刷新顺序，写入后超过 maxsize 就淘汰最久未使用项。
# 目的：不新增第三方依赖的前提下限制内存占用，并保留现有 dict 风格调用方式。
class LRUDict(OrderedDict):
    def __init__(self, maxsize=500):
        super().__init__()
        self._maxsize = maxsize

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self._maxsize:
            self.popitem(last=False)

    def get(self, key, default=None):
        if key in self:
            return self[key]
        return default


async def _resolve_oauth_api_key(app: "FastAPI", api_key: Optional[str], channel_id: Optional[str] = None) -> Optional[str]:
    """把指定渠道下的 OAuth key_id 解析成 access_token，静态 key 原样返回。"""
    # 修改原因：oauth_state.json 已按 provider name 分层，同一个 key_id 可能在多个渠道中存在不同凭据。
    # 修改方式：解析时要求调用方传入当前 provider['provider']，只在对应渠道下查找 key_id。
    # 目的：保持静态 key 向后兼容，同时避免 OAuth access_token 被跨渠道误解析。
    if not api_key or not channel_id or app is None or not hasattr(app, "state") or not hasattr(app.state, "oauth_manager"):
        return api_key
    resolved = await app.state.oauth_manager.resolve(channel_id, api_key)
    if resolved is not None:
        # 修改原因：OAuth resolve 只返回 access_token，但部分 channel adapter 还需要 project_id、email 等非敏感字段。
        # 修改方式：解析成功后从 OAuthManager 读取过滤后的 credential 元数据，写入当前请求上下文。
        # 目的：提供通用元数据透传机制，让各 adapter 自行决定是否读取这些字段。
        try:
            from core.middleware import request_info
            current_info = request_info.get()
            if isinstance(current_info, dict):
                current_info["_oauth_resolved"] = True
                metadata = app.state.oauth_manager.get_credential_metadata(channel_id, api_key)
                if metadata:
                    current_info["_oauth_credential_metadata"] = metadata
        except Exception:
            pass
        return resolved
    return api_key


def _fill_failure_provider_info(
    current_info: Dict[str, Any],
    provider_name: Optional[str],
    request_model_name: str,
) -> None:
    # 修改原因：错误路径过去只记录成功请求中的 provider 和 model，导致 500 日志显示“未知”或“-”。
    # 修改方式：在失败统计写入前统一补齐最后尝试的 provider、缺失的 provider_id 和缺失的 model。
    # 目的：让不重试错误和所有重试耗尽错误都能在日志中定位最后失败渠道与请求模型。
    provider_value = provider_name if provider_name else None
    current_info["provider"] = provider_value
    if not current_info.get("provider_id"):
        current_info["provider_id"] = provider_value
    if not current_info.get("model"):
        current_info["model"] = request_model_name


def _channel_stats_item_from_call(args: tuple, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 update_channel_stats 的调用参数转换为批量写入所需的字典。"""
    # 修改原因：consumer 收到的是旧函数签名的 *args/**kwargs，批量写入函数需要结构化条目。
    # 修改方式：兼容当前调用方使用的前四个位置参数，以及 success/provider_api_key 关键字参数。
    # 目的：保持 _fire_and_forget_channel_stats 对外签名不变，同时让内部可以批量落库。
    required_names = ("request_id", "provider", "model", "api_key", "success")
    values: Dict[str, Any] = {}
    for index, name in enumerate(required_names):
        if len(args) > index:
            values[name] = args[index]
        elif name in kwargs:
            values[name] = kwargs[name]
        else:
            return None
    values["provider_api_key"] = args[5] if len(args) > 5 else kwargs.get("provider_api_key")
    return values


async def _flush_channel_stats_batch(batch: List[tuple]) -> None:
    """批量刷新 channel stats，非标准调用回退到原函数逐条执行。"""
    # 修改原因：生产路径传入 core.stats.update_channel_stats，可以合并为一次批量写入；测试或扩展路径可能传入其他可调用对象。
    # 修改方式：只把标准 update_channel_stats 调用转换为 batch_update_channel_stats，其余调用仍逐条 await 原函数。
    # 目的：在修复生产协程泄漏和 SQLite 写放大的同时，不破坏旧的可调用参数兼容性。
    batch_items: List[Dict[str, Any]] = []
    fallback_calls: List[tuple] = []
    for func, args, kwargs in batch:
        if func is update_channel_stats:
            item = _channel_stats_item_from_call(args, kwargs)
            if item is not None:
                batch_items.append(item)
                continue
        fallback_calls.append((func, args, kwargs))

    if batch_items:
        try:
            await batch_update_channel_stats(batch_items)
        except Exception as e:
            logger.error(f"Error updating channel stats (batch): {e}")

    for func, args, kwargs in fallback_calls:
        try:
            await func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error updating channel stats: {str(e)}")


def _fire_and_forget_channel_stats(update_channel_stats_func: Callable, *args, **kwargs) -> None:
    """将统计数据放入 buffer，由常驻 consumer 批量写入。"""
    # 修改原因：每次调用都 create_task 会在 SQLite 串行写入和 database locked 重试时堆积协程。
    # 修改方式：这里只追加轻量参数到固定长度 deque，并确保单个 consumer 负责后续写入。
    # 目的：把后台任务数量固定为一个，极端情况下由 deque(maxlen=10000) 丢弃最旧统计兜底。
    _channel_stats_buffer.append((update_channel_stats_func, args, kwargs))
    _ensure_consumer_started()
    if len(_channel_stats_buffer) >= _CHANNEL_STATS_BATCH_SIZE and _channel_stats_flush_event is not None:
        _channel_stats_flush_event.set()


def _ensure_consumer_started() -> None:
    """确保 channel stats 常驻 consumer 已经启动。"""
    global _channel_stats_consumer_started, _channel_stats_flush_event
    # 修改原因：请求路径是同步函数，不能 await 启动后台任务，也不能为每条统计创建新任务。
    # 修改方式：在存在 running loop 时创建一个常驻 consumer，并复用同一 loop 上的唤醒事件。
    # 目的：既保持调用方无需修改，又避免启动或关闭阶段没有事件循环时抛出异常。
    if _channel_stats_consumer_started:
        return
    try:
        loop = asyncio.get_running_loop()
        _channel_stats_flush_event = asyncio.Event()
        loop.create_task(_channel_stats_consumer())
        _channel_stats_consumer_started = True
    except RuntimeError:
        pass


async def _channel_stats_consumer() -> None:
    """常驻后台任务：每 2 秒或攒满 50 条时批量写入。"""
    global _channel_stats_consumer_started, _channel_stats_flush_event
    # 修改原因：SQLite 写入需要串行化，逐请求后台协程会在锁等待时消耗大量内存。
    # 修改方式：consumer 按固定批量从 deque 取出统计项，并调用 batch_update_channel_stats 一次提交多条。
    # 目的：降低协程数量、事务次数和 fsync 次数，同时在任务取消时尽量 flush 剩余统计。
    try:
        while True:
            try:
                if _channel_stats_flush_event is None:
                    await asyncio.sleep(_CHANNEL_STATS_FLUSH_INTERVAL)
                else:
                    await asyncio.wait_for(
                        _channel_stats_flush_event.wait(),
                        timeout=_CHANNEL_STATS_FLUSH_INTERVAL,
                    )
                    _channel_stats_flush_event.clear()
            except asyncio.TimeoutError:
                pass

            if not _channel_stats_buffer:
                continue

            batch = []
            while _channel_stats_buffer and len(batch) < _CHANNEL_STATS_BATCH_SIZE:
                batch.append(_channel_stats_buffer.popleft())
            await _flush_channel_stats_batch(batch)
    except asyncio.CancelledError:
        while _channel_stats_buffer:
            batch = []
            while _channel_stats_buffer and len(batch) < _CHANNEL_STATS_BATCH_SIZE:
                batch.append(_channel_stats_buffer.popleft())
            await _flush_channel_stats_batch(batch)
    except Exception as e:
        logger.error(f"Channel stats consumer crashed: {e}")
    finally:
        _channel_stats_consumer_started = False
        _channel_stats_flush_event = None


def get_preference_value(provider_timeouts: Dict[str, Any], original_model: str) -> Optional[int]:
    """
    根据模型名获取偏好值（如超时时间）
    
    Args:
        provider_timeouts: 偏好配置字典
        original_model: 原始模型名
        
    Returns:
        偏好值，如果未找到则返回 None
    """
    timeout_value = None
    original_model = original_model.lower()
    if original_model in provider_timeouts:
        timeout_value = provider_timeouts[original_model]
    else:
        # 尝试模糊匹配模型
        for timeout_model in provider_timeouts:
            if timeout_model != "default" and timeout_model.lower() in original_model.lower():
                timeout_value = provider_timeouts[timeout_model]
                break
        else:
            # 如果模糊匹配失败，使用渠道的默认值
            timeout_value = provider_timeouts.get("default", None)
    return timeout_value


def get_preference(
    preference_config: Dict[str, Any],
    channel_id: str,
    original_request_model: tuple,
    default_value: int
) -> int:
    """
    获取偏好配置值（如超时时间、keepalive 间隔）
    
    按照 channel_id -> request_model_name -> original_model -> global default 的顺序查找
    
    Args:
        preference_config: 偏好配置字典
        channel_id: 渠道 ID
        original_request_model: (original_model, request_model_name) 元组
        default_value: 默认值
        
    Returns:
        偏好配置值
    """
    original_model, request_model_name = original_request_model
    provider_timeouts = safe_get(preference_config, channel_id, default=preference_config["global"])
    timeout_value = get_preference_value(provider_timeouts, request_model_name)
    if timeout_value is None:
        timeout_value = get_preference_value(provider_timeouts, original_model)
    if timeout_value is None:
        timeout_value = get_preference_value(preference_config["global"], original_model)
    if timeout_value is None:
        timeout_value = preference_config["global"].get("default", default_value)
    return timeout_value


def normalize_keepalive_interval(keepalive_interval: Any, timeout_value: Any) -> Optional[int]:
    """把 keepalive_interval 规范化为可用于 SSE 心跳的正整数秒数。"""
    # 修改原因：keepalive_interval 来自配置库，可能缺失、为 0/负数、字符串，或大于请求超时。
    # 修改方式：集中转换和边界检查，只有正整数且小于请求超时时才启用。
    # 目的：避免无效配置触发热循环/无意义心跳，同时让默认 15 秒心跳稳定生效。
    try:
        interval = int(keepalive_interval)
    except (TypeError, ValueError):
        return None

    if interval <= 0:
        return None

    try:
        timeout = int(timeout_value)
    except (TypeError, ValueError):
        timeout = 0

    if timeout > 0 and interval >= timeout:
        return None

    return interval


class ModelRequestHandler:
    """
    模型请求处理器
    
    负责根据配置选择 provider、发送请求、处理错误和重试逻辑。
    """
    
    def __init__(
        self,
        app: "FastAPI",
        request_info_getter: Callable[[], Dict[str, Any]],
        update_channel_stats_func: Callable,
        default_timeout: int = DEFAULT_TIMEOUT
    ):
        """
        初始化处理器
        
        Args:
            app: FastAPI 应用实例
            request_info_getter: 获取当前请求信息的函数
            update_channel_stats_func: 更新渠道统计的函数
            default_timeout: 默认超时时间
        """
        self.app = app
        self.request_info_getter = request_info_getter
        self.update_channel_stats_func = update_channel_stats_func
        self.default_timeout = default_timeout
        # 修改原因：defaultdict 会让游标表和锁表随着新 key 自动增长，长期运行会增加内存占用。
        # 修改方式：改用固定上限为 500 的 LRUDict；游标和锁在读取处显式处理缺失值。
        # 目的：保留现有轮转和并发控制行为，同时限制缓存规模。
        self.last_provider_indices = LRUDict(maxsize=500)
        self.locks = LRUDict(maxsize=500)

    async def _build_attempt_providers(
        self,
        providers: List[Dict[str, Any]],
        request_model_name: str,
        scheduling_algorithm: str,
        advance_cursor: bool = True,
    ) -> List[Dict[str, Any]]:
        """构造单次请求真正用于尝试的渠道列表。

        保留权重展开后的起点选择，但在一次请求内部去掉重复 provider，
        避免同一渠道因为权重槽位被重复尝试很多次。
        """
        if not providers:
            return []

        async def build_unique_group(provider_slots: List[Dict[str, Any]], cursor_key: str) -> List[Dict[str, Any]]:
            """在一个优先级组内执行原有轮转和去重逻辑。"""
            # 修改原因：虚拟路由的 fallback 组间顺序必须稳定，但同组仍要保留原权重槽位轮转语义。
            # 修改方式：把旧的全列表轮转和去重逻辑抽成组内函数，普通路由仍以整列表作为唯一分组。
            # 目的：不改 handler 重试循环，只确保它收到的尝试列表已经符合链条降级顺序。
            if not provider_slots:
                return []

            provider_names = [provider.get("provider") for provider in provider_slots]
            has_duplicate_slots = len(set(provider_names)) != len(provider_names)
            should_rotate_slots = scheduling_algorithm != "fixed_priority" or has_duplicate_slots

            start_index = 0
            if should_rotate_slots:
                # 修改原因：locks 改为 LRU 普通字典后，读取缺失 key 不会再自动创建 asyncio.Lock。
                # 修改方式：先用 get 查询，缺失时显式创建并写回，写入时由 LRUDict 控制容量。
                # 目的：保持同一 cursor_key 串行更新游标，同时避免锁表无上限增长。
                lock = self.locks.get(cursor_key)
                if lock is None:
                    lock = asyncio.Lock()
                    self.locks[cursor_key] = lock

                async with lock:
                    # 修改原因：last_provider_indices 改为 LRU 普通字典后，读取缺失 key 不会再自动写入默认值。
                    # 修改方式：用 get 读取当前游标，默认值沿用原 defaultdict(lambda: -1) 的行为。
                    # 目的：不改变首次轮转从第一个 provider 开始的逻辑，同时避免游标表无上限增长。
                    current_index = self.last_provider_indices.get(cursor_key, -1)
                    if advance_cursor:
                        current_index = (current_index + 1) % len(provider_slots)
                        self.last_provider_indices[cursor_key] = current_index
                    elif current_index < 0:
                        current_index = 0
                        self.last_provider_indices[cursor_key] = current_index
                    start_index = current_index % len(provider_slots)

            ordered_slots = provider_slots[start_index:] + provider_slots[:start_index]

            unique_group: List[Dict[str, Any]] = []
            seen_provider_names = set()
            for provider in ordered_slots:
                provider_name = provider.get("provider")
                if provider_name in seen_provider_names:
                    continue
                seen_provider_names.add(provider_name)
                unique_group.append(provider)
            return unique_group

        has_virtual_priorities = any(
            provider.get("_virtual_route_provider") and "_virtual_priority" in provider
            for provider in providers
        )
        if not has_virtual_priorities:
            return await build_unique_group(providers, request_model_name)

        # 修改原因：路由阶段会按 _virtual_priority 生成权重槽位，但旧构造逻辑可能因槽位轮转从 fallback 组开始。
        # 修改方式：构造阶段再次按 _virtual_priority 拆组，只在组内轮转和去重，再按 priority 升序合并。
        # 目的：让后续 while 重试循环自然先尝试完当前 chain 节点，再降级到下一个节点。
        priority_groups: Dict[int, List[Dict[str, Any]]] = {}
        for provider in providers:
            try:
                priority = int(provider.get("_virtual_priority", 0) or 0)
            except (TypeError, ValueError):
                priority = 0
            priority_groups.setdefault(priority, []).append(provider)

        unique_providers: List[Dict[str, Any]] = []
        seen_provider_names = set()
        for priority in sorted(priority_groups.keys()):
            cursor_key = f"{request_model_name}::virtual_priority::{priority}"
            for provider in await build_unique_group(priority_groups[priority], cursor_key):
                provider_name = provider.get("provider")
                if provider_name in seen_provider_names:
                    continue
                seen_provider_names.add(provider_name)
                unique_providers.append(provider)

        return unique_providers

    async def request_model(
        self,
        request_data: Union[RequestModel, ImageGenerationRequest, AudioTranscriptionRequest, ModerationRequest, EmbeddingRequest],
        api_index: int,
        background_tasks: BackgroundTasks,
        endpoint: Optional[str] = None,
        dialect_id: Optional[str] = None,
        original_payload: Optional[Dict[str, Any]] = None,
        original_headers: Optional[Dict[str, str]] = None,
        passthrough_only: bool = False,
        override_providers: Optional[List[Dict[str, Any]]] = None,
        force_api_key: Optional[str] = None,
        override_auto_retry: Optional[bool] = None,
        override_timeout: Optional[int] = None,
    ) -> Response:
        """
        处理模型请求
        
        Args:
            request_data: 请求数据
            api_index: API key 索引
            background_tasks: 后台任务
            endpoint: 请求端点
            dialect_id: 入口方言 ID（原生路由传入）
            original_payload: 原始 native 请求体（透传用）
            original_headers: 原始请求头（透传用）
            override_auto_retry: override provider 测试是否允许按候选列表自动重试
            
        Returns:
            响应对象
        """
        config = self.app.state.config
        request_model_name = request_data.model

        # ── override 模式：跳过用户限速和全局路由（测试/直接调用场景） ──
        if override_providers is not None:
            matching_providers = override_providers
            num_matching_providers = len(matching_providers)
            if num_matching_providers == 0:
                raise HTTPException(status_code=400, detail="No providers specified for test")
            scheduling_algorithm = "fixed_priority"
            # 修改原因：普通单渠道测试应只测当前渠道，但虚拟路由测试需要按 chain 候选继续尝试后续节点。
            # 修改方式：保留默认不重试；只有调用方显式传 override_auto_retry=True 时才开启 override provider 列表内重试。
            # 目的：不改变现有渠道测试语义，同时让虚拟模型测试能覆盖完整 fallback 链条。
            auto_retry = bool(override_auto_retry)
            role = "test"
        else:
            # ── 正常路径：用户限速 + 全局路由 ──
            try:
                final_api_key = self.app.state.api_list[api_index]
                await self.app.state.user_api_keys_rate_limit[final_api_key].next(request_model_name)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(status_code=429, detail="Too many requests")

            if not safe_get(config, 'api_keys', api_index, 'model'):
                raise HTTPException(status_code=404, detail=f"No matching model found: {request_model_name}")

            scheduling_algorithm = safe_get(
                config, 'api_keys', api_index, "preferences", "SCHEDULING_ALGORITHM",
                default=safe_get(config, "preferences", "SCHEDULING_ALGORITHM", default="fixed_priority")
            )

            request_total_tokens = 0
            if request_data and isinstance(request_data, RequestModel):
                for message in request_data.messages:
                    if message.content and isinstance(message.content, str):
                        request_total_tokens += len(message.content)
            request_total_tokens = int(request_total_tokens / 4)

            matching_providers = await get_right_order_providers(
                request_model_name, config, api_index, scheduling_algorithm, 
                self.app, request_total_tokens=request_total_tokens
            )
            matching_providers = await self._build_attempt_providers(
                matching_providers,
                request_model_name=request_model_name,
                scheduling_algorithm=scheduling_algorithm,
                advance_cursor=True,
            )
            num_matching_providers = len(matching_providers)

            auto_retry = safe_get(config, 'api_keys', api_index, "preferences", "AUTO_RETRY", default=True)
            role = safe_get(
                config, 'api_keys', api_index, "role", 
                default=safe_get(config, 'api_keys', api_index, "api", default="None")[:8]
            )

        # 修改原因：标准路由没有原始 FastAPI Request 参数，BYOK 真实 key 只能从鉴权阶段写入的请求上下文读取。
        # 修改方式：只把 request_info/ContextVar 中的 BYOK 上下文识别为 BYOK；显式 force_api_key 继续保留为渠道测试或直接调用用 key。
        # 目的：BYOK provider 在真实请求中能拿到用户上游 key，同时避免单渠道测试的显式 key 被误当作 BYOK 而跳过重试和统计。
        current_info_for_byok_lookup = self.request_info_getter()
        byok_real_key = (
            current_info_for_byok_lookup.get("_byok_real_key")
            or current_info_for_byok_lookup.get("byok_real_key")
            or get_byok_real_key()
        )
        byok_template_key = (
            current_info_for_byok_lookup.get("_byok_template_key")
            or current_info_for_byok_lookup.get("byok_template_key")
            or get_byok_template_key()
        )
        if byok_template_key:
            current_info_for_byok = self.request_info_getter()
            current_info_for_byok["api_key"] = byok_template_key

        status_code = 500
        error_message = None

        index = 0
        # 获取配置的最大重试次数上限，默认为 10
        # [已废弃] max_retry_count 机制已移除，重试终止完全靠 is_all_rate_limited 兜底
        # max_retry_limit = safe_get(config, 'preferences', 'max_retry_count', default=0)

        # 计算最大尝试次数（包含首轮 + 自动重试）。
        # 修复：
        # - 使用 get_enabled_items_count 排除禁用 key。
        #   容易在“只有 1 个可用 key，但配置里堆了大量禁用 key”时触发 1000+ 次重试。
        # - 统一按“启用的 key 数量”计算。
        def _provider_key_slots(p: Dict[str, Any]) -> int:
            """返回该 provider 可用于重试的 key 数量（至少为 1）。

            注意：没有配置 api（例如无需 key 的渠道）也按 1 计。
            """
            if is_byok_provider(p):
                # 修改原因：BYOK provider 没有本地 circular list，api: ["*"] 只是模式标记。
                # 修改方式：重试槽位固定按 1 计算，不读取 provider_api_circular_list。
                # 目的：避免 "*" 被视作本地 key，也避免缺失 key pool 影响重试次数计算。
                return 1
            try:
                # 修改原因：provider_api_circular_list 已改为普通 dict，读取缺失 provider 时不能再隐式创建空 key 池。
                # 修改方式：使用 get 读取现有循环列表，缺失时按 0 个启用 key 处理。
                # 目的：保持重试次数下限逻辑不变，同时避免读路径产生长期驻留对象。
                circular_list = provider_api_circular_list.get(p["provider"])
                enabled = circular_list.get_enabled_items_count() if circular_list else 0
            except Exception:
                enabled = 0
            try:
                enabled_int = int(enabled)
            except (TypeError, ValueError):
                enabled_int = 0
            return max(1, enabled_int)

        def _calc_retry_count(providers: List[Dict[str, Any]]) -> int:
            """计算“额外重试次数”。

            设计目标：
            - 保持原有语义：总尝试次数 ≈ num_matching_providers + retry_count
            不设人为上限，终止靠 is_all_rate_limited 兜底
            - 仅按“启用的 key 数量”估算，避免禁用 key 造成 retry_count 虚高
            """
            n = len(providers)
            if n <= 0:
                return 0

            if n == 1:
                slots = _provider_key_slots(providers[0])
                # 单 provider：至少允许 1 次重试；若有多 key，可覆盖更多 key
                base = slots if slots > 1 else 1
                return base

            total_slots = sum(_provider_key_slots(p) for p in providers)
            tmp_retry_count = total_slots * 2
            return tmp_retry_count

        # ── 入站拦截器：鉴权完成、provider 选择前 ──
        try:
            from core.plugins.interceptors import apply_inbound_interceptors
            _inbound_api_key_info = {
                "api_key": self.app.state.api_list[api_index] if not override_providers else None,
                "api_index": api_index,
                "model": request_model_name,
                "role": role,
            }
            _inbound_enabled_plugins = safe_get(
                config, 'api_keys', api_index, 'preferences', 'enabled_plugins',
                default=None
            ) if not override_providers else None
            request_data = await apply_inbound_interceptors(
                request_data, None, _inbound_api_key_info, _inbound_enabled_plugins
            )
        except Exception as _inbound_err:
            logger.warning(f"Inbound interceptors error: {_inbound_err}")

        retry_count = _calc_retry_count(matching_providers)
        max_attempts = min(num_matching_providers + retry_count, 500)  # 绝对上限防死循环

        # 初始化重试路径记录
        retry_path: List[Dict[str, Any]] = []
        current_retry_count = 0

        # ── 虚拟路由优先级分组索引 ──
        # matching_providers 已按 _virtual_priority 升序排列（0,0,0,1,1,2...）
        # 构建 priority_group_ranges: [(start, end, priority), ...]
        # 用于重试循环中强制在同一 priority group 内走到黑，再降级
        _is_virtual_route = any(
            p.get("_virtual_route_provider") and "_virtual_priority" in p
            for p in matching_providers
        )
        _priority_group_ranges: list = []  # [(start_idx, end_idx_exclusive, priority)]
        if _is_virtual_route:
            _cur_priority = None
            _group_start = 0
            for _i, _p in enumerate(matching_providers):
                _pp = int(_p.get("_virtual_priority", 0) or 0)
                if _cur_priority is not None and _pp != _cur_priority:
                    _priority_group_ranges.append((_group_start, _i, _cur_priority))
                    _group_start = _i
                _cur_priority = _pp
            if _cur_priority is not None:
                _priority_group_ranges.append((_group_start, len(matching_providers), _cur_priority))

        def _get_priority_group_range(idx: int):
            """返回 idx 所在 priority group 的 (start, end) 范围"""
            for start, end, _ in _priority_group_ranges:
                if start <= idx < end:
                    return start, end
            return 0, num_matching_providers

        while True:
            if index >= max_attempts:
                break
            current_index = index % num_matching_providers
            index += 1
            provider = matching_providers[current_index]

            provider_name = provider['provider']
            provider_is_byok = is_byok_provider(provider)

            # 检查是否所有 API 密钥都被速率限制
            model_dict = provider["_model_dict_cache"]
            original_model = model_dict[request_model_name]
            # 修改原因：provider_api_circular_list 改为普通 dict 后，读不存在的 provider 不应自动创建空列表。
            # 修改方式：先用 get 取得当前 provider 的循环列表，缺失时跳过限流耗尽检查。
            # 目的：保留原有可用 provider 流程，同时避免缺失配置造成内存增长。
            provider_circular_list = provider_api_circular_list.get(provider_name)
            # 修改原因：BYOK provider 没有本地 key pool，api: ["*"] 也不能进入本地限流耗尽判断。
            # 修改方式：只有非 BYOK provider 且存在 circular list 时才调用 is_all_rate_limited。
            # 目的：避免 "*" 被本地限流、冷却或禁用逻辑当成真实 provider key 处理。
            if not override_providers and provider_circular_list and not provider_is_byok:
                if await provider_circular_list.is_all_rate_limited(original_model):
                    error_message = "All API keys are rate limited and stop auto retry!"
                    if num_matching_providers == 1:
                        break
                    # 虚拟路由：检查同 priority group 是否全部耗尽
                    if _is_virtual_route:
                        grp_start, grp_end = _get_priority_group_range(current_index)
                        group_all_exhausted = True
                        for gi in range(grp_start, grp_end):
                            gi_provider = matching_providers[gi]
                            if is_byok_provider(gi_provider):
                                # 修改原因：BYOK provider 不依赖本地 key pool，不能被视为本地 key 全部限流。
                                # 修改方式：虚拟路由分组检查遇到 BYOK provider 时直接认为该组未被本地限流耗尽。
                                # 目的：避免缺失 circular list 或 "*" 占位符导致 BYOK provider 被错误跳过。
                                group_all_exhausted = False
                                break
                            gi_provider_name = gi_provider["provider"]
                            gi_circular_list = provider_api_circular_list.get(gi_provider_name)
                            if gi_circular_list and not await gi_circular_list.is_all_rate_limited(original_model):
                                group_all_exhausted = False
                                break
                        if not group_all_exhausted:
                            # 同组还有可用渠道 → 跳到组内下一个，不降级
                            continue
                        # 同组全耗尽 → 跳过整个组，直接到下一个 priority group
                        index = (grp_end % num_matching_providers) if grp_end < num_matching_providers else grp_end
                    continue

            original_request_model = (original_model, request_data.model)
            
            # 处理本地聚合器 Key 代理
            if is_local_api_key(provider_name) and provider_name in self.app.state.api_list:
                local_provider_api_index = self.app.state.api_list.index(provider_name)
                local_provider_scheduling_algorithm = safe_get(
                    config, 'api_keys', local_provider_api_index, "preferences", 
                    "SCHEDULING_ALGORITHM", default="fixed_priority"
                )
                local_provider_matching_providers = await get_right_order_providers(
                    request_model_name, config, local_provider_api_index, 
                    local_provider_scheduling_algorithm, self.app, 
                    request_total_tokens=request_total_tokens
                )
                local_timeout_value = 0
                for local_provider in local_provider_matching_providers:
                    local_provider_name = local_provider['provider']
                    if not is_local_api_key(local_provider_name):
                        local_timeout_value += get_preference(
                            self.app.state.provider_timeouts, local_provider_name, 
                            original_request_model, self.default_timeout
                        )
                local_provider_num_matching_providers = len(local_provider_matching_providers)
            else:
                local_timeout_value = get_preference(
                    self.app.state.provider_timeouts, provider_name, 
                    original_request_model, self.default_timeout
                )
                local_provider_num_matching_providers = 1

            local_timeout_value = local_timeout_value * local_provider_num_matching_providers

            # override_timeout 覆盖渠道默认 timeout（测试场景用）
            if override_timeout is not None and override_timeout > 0:
                local_timeout_value = override_timeout

            # 修改原因：keepalive_interval 默认值需要与启动初始化保持一致，避免运行时回退到关闭心跳。
            # 修改方式：把 get_preference 的 fallback 从 99999 改为 15 秒。
            # 目的：没有显式配置时默认发送 SSE 心跳，降低流式连接空闲断开的风险。
            keepalive_interval = get_preference(
                self.app.state.keepalive_interval, provider_name, 
                original_request_model, 15
            )
            keepalive_interval = normalize_keepalive_interval(keepalive_interval, local_timeout_value)
            if is_local_api_key(provider_name):
                keepalive_interval = None

            try:
                passthrough_ctx = None
                if dialect_id and original_payload is not None and isinstance(request_data, RequestModel):
                    from core.dialects.passthrough import evaluate_passthrough
                    passthrough_ctx = await evaluate_passthrough(
                        dialect_id=dialect_id,
                        original_payload=original_payload,
                        original_headers=original_headers or {},
                        target_provider=provider,
                        request_model=request_model_name,
                    )

                # passthrough_only 前置拦截：如果该端点仅支持透传，
                # 但当前 provider 与入口方言不匹配（透传未启用），
                # 直接跳过该 provider，不发送任何真实上游请求。
                if passthrough_only and not (passthrough_ctx and passthrough_ctx.enabled):
                    error_message = f"Endpoint {endpoint} requires passthrough mode, but provider {provider_name} is not compatible"
                    status_code = 501
                    continue

                process_fn = process_request_passthrough if (passthrough_ctx and passthrough_ctx.enabled) else process_request
                # 修改原因：只有 BYOK provider 才应使用请求中的真实上游 key；普通 provider 仍按配置 key pool 轮换。
                # 修改方式：按当前 provider 是否为 BYOK provider 计算 effective_force_api_key，并传给普通和透传路径。
                # 目的：避免 BYOK key 误覆盖非 BYOK provider，同时让透传路径也支持 BYOK。
                effective_force_api_key = byok_real_key if (byok_real_key and is_byok_provider(provider)) else force_api_key
                response = await process_fn(
                    request_data, provider, background_tasks, self.app,
                    self.request_info_getter, self.update_channel_stats_func,
                    passthrough_ctx=passthrough_ctx,
                    endpoint=endpoint,
                    role=role,
                    timeout_value=local_timeout_value,
                    keepalive_interval=keepalive_interval,
                    force_api_key=effective_force_api_key,
                ) if process_fn is process_request_passthrough else await process_request(
                    request_data, provider, background_tasks, self.app,
                    self.request_info_getter, self.update_channel_stats_func,
                    endpoint, role, local_timeout_value, keepalive_interval,
                    force_api_key=effective_force_api_key
                )

                # 成功时记录重试路径和重试次数
                current_info = self.request_info_getter()
                if retry_path:
                    current_info["retry_path"] = json.dumps(retry_path, ensure_ascii=False)
                current_info["retry_count"] = current_retry_count
                return response
            except asyncio.CancelledError:
                # 客户端取消请求，直接向上抛出，不再重试
                logger.info(f"Request cancelled by client for model {request_model_name}")
                raise
            except (Exception, HTTPException, httpx.ReadError,
                    httpx.RemoteProtocolError, httpx.LocalProtocolError, httpx.ReadTimeout,
                    httpx.ConnectError) as e:
                # 记录重试路径
                current_retry_count += 1
                
                # 获取完整的错误详情
                error_details = getattr(e, "detail", None) if isinstance(e, HTTPException) else None
                if isinstance(error_details, (dict, list)):
                    try:
                        full_error = json.dumps(error_details, ensure_ascii=False)
                    except Exception:
                        full_error = str(error_details)
                elif isinstance(e, HTTPException):
                    full_error = str(error_details) if error_details is not None else str(e)
                else:
                    full_error = str(e)

                retry_path.append({
                    "provider": provider_name,
                    "error": full_error[:2000],  # 增加错误信息长度限制到 2000 字符
                    "status_code": None  # 稍后更新
                })

                # 根据异常类型设置状态码和错误消息
                if isinstance(e, httpx.ReadTimeout):
                    status_code = 504  # Gateway Timeout
                    timeout_value = e.request.extensions.get('timeout', {}).get('read', -1)
                    error_message = f"Request timed out after {timeout_value} seconds"
                elif isinstance(e, httpx.ConnectError):
                    status_code = 503  # Service Unavailable
                    error_message = "Unable to connect to service"
                elif isinstance(e, httpx.ReadError):
                    status_code = 502  # Bad Gateway
                    error_message = "Network read error"
                elif isinstance(e, httpx.RemoteProtocolError):
                    status_code = 502  # Bad Gateway
                    error_message = "Remote protocol error"
                    
                    # 检测 HTTP/2 StreamReset 错误，自动重置连接池
                    error_str = str(e)
                    if "StreamReset" in error_str or "stream_id" in error_str:
                        try:
                            # 从 provider 的 base_url 提取 host 并重置连接
                            base_url = provider.get('base_url', '')
                            if base_url:
                                host = urlparse(base_url).netloc
                                if host and hasattr(self.app.state, 'client_manager'):
                                    await self.app.state.client_manager.reset_client(host)
                                    logger.info(f"Auto-reset HTTP/2 connection for {host} due to StreamReset error")
                        except Exception as reset_err:
                            logger.warning(f"Failed to auto-reset connection: {reset_err}")
                elif isinstance(e, httpx.LocalProtocolError):
                    status_code = 502  # Bad Gateway
                    error_message = "Local protocol error"
                elif isinstance(e, HTTPException):
                    status_code = e.status_code
                    # 错误解析应尽量由各渠道适配器完成，这里只做通用兜底。
                    error_message = str(getattr(e, "detail", None) or str(e))
                else:
                    status_code = 500  # Internal Server Error
                    error_message = str(e) or f"Unknown error: {e.__class__.__name__}"

                # ── Key Rules 统一错误处理 ──
                from core.key_rules import apply_key_rule_retry_override, resolve_key_rules, match_key_rules
                # 修改原因：BYOK provider 没有本地 key pool，上游 401/403 只说明用户自己的 key 有问题。
                # 修改方式：BYOK 请求跳过 key_rules 匹配和后续自动禁用/冷却。
                # 目的：避免把不存在的 provider key pool 冷却或禁用，也避免真实 key 出现在运行时禁用快照。
                is_current_byok_request = bool(byok_real_key) and is_byok_provider(provider)
                _key_rules = [] if is_current_byok_request else resolve_key_rules(provider.get("preferences") or {})
                _rule_result = match_key_rules(_key_rules, status_code, error_message) if _key_rules else None

                # 规则中的 remap: 把上游非标准状态码映射为标准码
                if _rule_result and _rule_result.get("remap"):
                    _mapped = _rule_result["remap"]
                    if 100 <= _mapped <= 599:
                        status_code = _mapped

                exclude_error_rate_limit = [
                    "BrokenResourceError",
                    "Proxy connection timed out",
                    "Unknown error: EndOfStream",
                    "'status': 'INVALID_ARGUMENT'",
                    "Unable to connect to service",
                    "Connection closed unexpectedly",
                    "Invalid JSON payload received. Unknown name ",
                    "User location is not supported for the API use",
                    "The model is overloaded. Please try again later.",
                    "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure (_ssl.c:1007)",
                    "<title>Worker exceeded resource limits",
                ]

                channel_id = provider['provider']

                # ★ 修复：优先从 request_info 获取本次实际使用的 api_key，
                # 避免并发场景下 after_next_current() 返回其他请求的 key，
                # 导致冷却/禁用操作作用在错误的 key 上。
                _current_info_for_key = self.request_info_getter()
                # 修改原因：provider_api_circular_list 已不再自动创建缺失 provider 的 key 池。
                # 修改方式：用 get 复用当前渠道循环列表；只有列表存在时才回退读取 after_next_current。
                # 目的：错误处理路径仍能定位实际 key，同时避免缺失渠道名生成空对象。
                channel_circular_list = provider_api_circular_list.get(channel_id)
                current_api = _current_info_for_key.get("_used_api_key")
                if not current_api and channel_circular_list:
                    current_api = await channel_circular_list.after_next_current()

                should_consider_channel_cooldown = (
                    not is_current_byok_request
                    and self.app.state.channel_manager.cooldown_period > 0
                    and override_providers is None
                    and num_matching_providers > 1
                    and all(error not in error_message for error in exclude_error_rate_limit)
                )

                # 仅统计"启用"的 key 数量，避免禁用 key 造成误判。
                if channel_circular_list:
                    try:
                        api_key_count_before_rule = channel_circular_list.get_enabled_items_count()
                    except Exception:
                        api_key_count_before_rule = channel_circular_list.get_items_count()
                else:
                    api_key_count_before_rule = 0

                key_rule_disabled_current = False
                # ── 应用 Key Rules 规则：冷却 / 禁用 ──
                if _rule_result and current_api and channel_circular_list and not is_current_byok_request:
                    _duration = _rule_result.get("duration", 0)
                    _reason = _rule_result.get("reason", "key_rule")
                    if _duration == -1:
                        # 永久禁用
                        await channel_circular_list.set_auto_disabled(
                            current_api, duration=0, reason=_reason
                        )
                        key_rule_disabled_current = True
                    elif _duration > 0:
                        # 修改原因：旧逻辑只在多 key 时冷却，单渠道单 key 依赖原有重试行为；但多渠道降级时，最后一个失败 key 必须能让渠道被判定为耗尽。
                        # 修改方式：多 key 仍按旧规则冷却；当渠道级降级条件成立时，最后一个 key 也执行冷却。
                        # 目的：既保留单渠道单 key 的旧行为，又让多渠道虚拟路由能在所有 key 不可用后再进入 fallback。
                        if (
                            (api_key_count_before_rule > 1 or should_consider_channel_cooldown)
                            and all(error not in error_message for error in exclude_error_rate_limit)
                        ):
                            await channel_circular_list.set_auto_disabled(
                                current_api, duration=_duration, reason=_reason
                            )
                            key_rule_disabled_current = True

                # 仅统计"启用"的 key 数量，避免禁用 key 造成误判。
                # 修改原因：旧逻辑先冷却渠道再冷却 key，会把同渠道剩余 key 从候选列表中埋没。
                # 修改方式：key 级规则执行后，再检查该渠道是否还有启用 key。
                # 目的：只有当前渠道所有 key 都不可用时，才进入渠道级冷却和候选列表重建。
                if channel_circular_list:
                    try:
                        api_key_count = channel_circular_list.get_enabled_items_count()
                    except Exception:
                        api_key_count = channel_circular_list.get_items_count()
                else:
                    api_key_count = 0

                should_rebuild_after_channel_cooldown = (
                    should_consider_channel_cooldown
                    and (api_key_count <= 0 or not key_rule_disabled_current)
                )

                if should_rebuild_after_channel_cooldown:
                    # 修改原因：只有当前错误确实触发 key 禁用时，才应等待当前渠道所有 key 耗尽；未命中 key_rules 时继续留在当前渠道会反复取到同一 key。
                    # 修改方式：key 已被禁用时沿用“启用 key 数量为 0 才冷却渠道”；key 未被禁用时立即走渠道级冷却并重建候选列表。
                    # 目的：既保留多 key 渠道先耗尽 key 的行为，又避免无匹配 key_rules 的渠道在虚拟路由中循环重试。
                    await self.app.state.channel_manager.exclude_model(channel_id, request_model_name)
                    matching_providers = await get_right_order_providers(
                        request_model_name, config, api_index, scheduling_algorithm,
                        self.app, request_total_tokens=request_total_tokens
                    )
                    matching_providers = await self._build_attempt_providers(
                        matching_providers,
                        request_model_name=request_model_name,
                        scheduling_algorithm=scheduling_algorithm,
                        advance_cursor=False,
                    )
                    last_num_matching_providers = num_matching_providers
                    num_matching_providers = len(matching_providers)
                    # provider 列表发生变化（或重新排序）时，重算最大尝试次数
                    retry_count = _calc_retry_count(matching_providers)
                    max_attempts = min(num_matching_providers + retry_count, 500)  # 绝对上限防死循环
                    if num_matching_providers != last_num_matching_providers:
                        index = 0
                # 当 key 被冷却但渠道仍有可用 key 时：不做 exclude_model，也不回退 index。
                # index 正常前进，通过 max_attempts 的 modulo 循环回来时 circular_list 自动取下一个 key。
                # 不能 index = current_index，否则 index 永远到不了 max_attempts，死循环。

                # 有些错误并没有请求成功，所以需要删除请求记录
                # 修改原因：直接访问 requests[current_api][original_model] 会在没有记录时创建空 deque。
                # 修改方式：先通过 get 读取 provider、api_key、model 三层对象，只有已有记录时才 pop。
                # 目的：兼容 deque 的同时避免错误路径制造新的空 requests 索引。
                provider_circular_list_for_requests = provider_api_circular_list.get(provider_name)
                api_request_map = provider_circular_list_for_requests.requests.get(current_api) if provider_circular_list_for_requests and current_api else None
                model_requests = api_request_map.get(original_model) if api_request_map else None
                if (current_api
                    and any(error in error_message for error in exclude_error_rate_limit)
                    and model_requests):
                    model_requests.pop()

                # 根据错误消息调整状态码
                if "string_above_max_length" in error_message:
                    status_code = 413
                if "must be less than max_seq_len" in error_message:
                    status_code = 413
                if "Please reduce the length of the messages or completion" in error_message:
                    status_code = 413
                if "Request contains text fields that are too large." in error_message:
                    status_code = 413
                # openrouter
                if "Please reduce the length of either one, or use the" in error_message:
                    status_code = 413
                # gemini
                if "exceeds the maximum number of tokens allowed" in error_message:
                    status_code = 413
                if ("'reason': 'API_KEY_INVALID'" in error_message 
                    or "API key not valid" in error_message 
                    or "API key expired" in error_message):
                    status_code = 401
                if "User location is not supported for the API use." in error_message:
                    status_code = 403
                if "<center><h1>400 Bad Request</h1></center>" in error_message:
                    status_code = 502
                if "The response was filtered due to the prompt triggering Azure OpenAI's content management policy." in error_message:
                    status_code = 403
                if "<head><title>413 Request Entity Too Large</title></head>" in error_message:
                    status_code = 429

                # 修改原因：BYOK 失败日志不能输出用户真实上游 key。
                # 修改方式：BYOK 请求只打印模板身份或空值，普通请求保留原有 current_api 便于排障。
                # 目的：上游 401/403 等错误返回给用户即可，不在服务端日志泄露 BYOK key。
                log_api_key = byok_template_key if (byok_real_key and is_byok_provider(provider)) else current_api
                logger.error(f"Error {status_code} with provider {channel_id} API key: {log_api_key}: {error_message}")
                if is_debug:
                    import traceback
                    traceback.print_exc()

                # 更新重试路径中的状态码
                if retry_path:
                    retry_path[-1]["status_code"] = status_code

                retry_enabled = (
                    auto_retry
                    and not is_current_byok_request
                    and (
                        status_code not in [400, 413, 401, 403]
                        or urlparse(provider.get('base_url', '')).netloc == 'models.inference.ai.azure.com'
                    )
                )

                # 修改原因：Key Rules 新增 retry 三态，需要在默认硬编码判断之后提供按规则覆盖的能力。
                # 修改方式：只有 _rule_result.retry 为 bool 时覆盖 retry_enabled，缺失时保持默认结果。
                # 目的：支持 retry=true 强制允许重试、retry=false 强制禁止重试，同时不影响旧配置。
                retry_enabled = apply_key_rule_retry_override(_rule_result, retry_enabled)

                # 测试模式（override + auto_retry=False）：key_rule 的冷却/禁用照常执行，但不重试
                if not auto_retry:
                    retry_enabled = False

                # 特定场景禁止重试：
                # 1. 图像生成失败（no image was generated）通常是内容审核或模型能力问题，重试无效且增加负载
                if "no image was generated" in error_message.lower():
                    retry_enabled = False
                
                # 2. 图像模型遇到 429，通常意味着高并发触发了严格配额，重试会放大负载
                is_image_model = "-image" in request_model_name.lower() or "image-generation" in request_model_name.lower()
                if is_image_model and status_code == 429:
                    retry_enabled = False

                # 若还有剩余尝试次数，则进行自动重试
                if retry_enabled and index < max_attempts:
                    if status_code in {429, 500, 502, 503, 504}:
                        base_delay = 0.5 if status_code == 429 else 0.2
                        # current_retry_count 从 1 开始；最多指数到 2^5，再封顶 5 秒
                        delay = min(5.0, base_delay * (2 ** min(max(current_retry_count - 1, 0), 5)))
                        await asyncio.sleep(delay)
                    # 虚拟路由：重试时强制留在同一 priority group 内
                    if _is_virtual_route and _priority_group_ranges:
                        grp_start, grp_end = _get_priority_group_range(current_index)
                        next_idx = index % num_matching_providers
                        if next_idx >= grp_end or next_idx < grp_start:
                            # 即将越过当前 group → 检查组内是否还有可用 key
                            group_has_available = False
                            for gi in range(grp_start, grp_end):
                                gi_provider = matching_providers[gi]
                                if is_byok_provider(gi_provider):
                                    # 修改原因：BYOK provider 没有本地限流状态，不能因为没有 circular list 就被视为不可用。
                                    # 修改方式：虚拟路由重试检查遇到 BYOK provider 时直接认为组内仍有可用渠道。
                                    # 目的：避免 "*" 占位符进入 key pool，同时保持 BYOK provider 的路由兼容性。
                                    group_has_available = True
                                    break
                                gi_name = gi_provider["provider"]
                                # 修改原因：provider_api_circular_list 改为普通 dict 后，读取缺失 provider 需要显式判空。
                                # 修改方式：使用 get 取得已有循环列表，再执行 is_all_rate_limited 检查。
                                # 目的：避免虚拟路由重试检查创建空 key 池。
                                gi_circular_list = provider_api_circular_list.get(gi_name)
                                if gi_circular_list:
                                    if not await gi_circular_list.is_all_rate_limited(original_model):
                                        group_has_available = True
                                        break
                            if group_has_available:
                                index = grp_start  # 回到组头继续试
                    continue

                # retry_enabled 但已无重试额度：跳出循环，走统一的“所有重试失败”出口
                if retry_enabled and index >= max_attempts:
                    break

                # 不重试：直接返回本次错误
                # 失败时也记录重试信息和统计
                current_info = self.request_info_getter()
                # 修改原因：不重试错误会直接写入失败统计，过去没有补齐渠道和模型字段。
                # 修改方式：在写 retry_path 和失败状态前调用统一 helper，写入最后尝试 provider 与请求模型。
                # 目的：避免直接返回 500 或其他错误时日志 provider 显示“未知”、model 显示“-”。
                _fill_failure_provider_info(current_info, provider_name, request_model_name)
                if retry_path:
                    current_info["retry_path"] = json.dumps(retry_path, ensure_ascii=False)
                current_info["retry_count"] = current_retry_count
                current_info["success"] = False
                current_info["status_code"] = status_code
                # 记录处理时间
                if "start_time" in current_info:
                    process_time = time() - current_info["start_time"]
                    current_info["process_time"] = process_time
                # 写入失败统计
                # 修改原因：BackgroundTasks 仍会为每条失败统计创建待执行任务，SQLite 锁等待时会继续堆积。
                # 修改方式：改为同步 enqueue_stats 入队，由 core.stats 的单个 consumer 批量写入。
                # 目的：让不重试失败路径不再增加后台协程数量，同时保留失败统计。
                enqueue_stats(current_info, app=self.app)
                return openai_error_response(
                    f"Error: Current provider response failed: {error_message}",
                    status_code,
                )

        # 所有重试都失败
        current_info = self.request_info_getter()
        current_info["first_response_time"] = -1
        current_info["success"] = False
        current_info["status_code"] = status_code
        # 修改原因：所有重试失败时旧逻辑把 provider 写成 None，丢失最后一次尝试的渠道。
        # 修改方式：复用失败字段补全 helper，只在 provider_id 和 model 缺失时补写它们。
        # 目的：让重试耗尽后的日志至少保留最后失败渠道和原始请求模型。
        _fill_failure_provider_info(current_info, provider_name, request_model_name)
        # 记录最终的重试信息
        if retry_path:
            current_info["retry_path"] = json.dumps(retry_path, ensure_ascii=False)
        current_info["retry_count"] = current_retry_count
        # 记录处理时间
        if "start_time" in current_info:
            process_time = time() - current_info["start_time"]
            current_info["process_time"] = process_time
        # 写入失败统计
        # 修改原因：所有重试失败路径可能在高并发下集中进入 SQLite 写入，BackgroundTasks 会放大协程积压。
        # 修改方式：改为同步 enqueue_stats 入队，并交给 request_stats consumer 批量提交。
        # 目的：保留重试耗尽后的失败日志，同时避免请求结束阶段阻塞事件循环。
        enqueue_stats(current_info, app=self.app)
        return openai_error_response(
            f"All {request_data.model} error: {error_message}",
            status_code,
        )
