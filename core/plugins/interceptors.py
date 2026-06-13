"""
请求/响应拦截器系统

提供 request、response 和 balance_enricher 三个扩展点，允许插件在请求发送前、响应返回后和余额查询后进行拦截、处理或补充。

支持插件参数：
- enabled_plugins 格式：["plugin_name:options", "plugin_name", ...]
- 例如：["gthink:max", "claude_thinking", "my_plugin:foo,bar"]
- 插件内部通过 parse_plugin_options() 或 get_plugin_options() 读取参数

使用方式：
```python
from core.plugins.interceptors import (
    register_request_interceptor,
    register_response_interceptor,
    register_balance_enricher,
    get_plugin_options,
)

# 注册请求拦截器
async def my_request_interceptor(request, engine, provider, api_key, url, headers, payload):
    # 读取插件参数（从 provider 中解析）
    options = get_plugin_options("my_plugin", provider)
    # options 是字符串，如 "max" 或 "foo,bar"，插件自行解析
    
    if options == "max":
        payload["custom_param"] = 9999
    elif options:
        parts = options.split(",")
        # ...
    
    return url, headers, payload

register_request_interceptor("my_plugin", my_request_interceptor, priority=50)

# 注册响应拦截器
async def my_response_interceptor(response_chunk, engine, model, is_stream):
    # 处理响应
    return response_chunk

register_response_interceptor("my_plugin", my_response_interceptor, priority=50)

# 注册余额补充器
async def my_balance_enricher(result, engine, provider):
    # 往余额查询结果中补充自定义字段
    result["extra"] = "value"
    return result

register_balance_enricher("my_plugin_balance", my_balance_enricher, priority=50)
```
"""

from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, field
import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager
import re

from ..log_config import logger


# ==================== 插件参数解析工具 ====================

# 响应拦截器和余额补充器调用期间的 enabled_plugins 上下文
# 修改原因：balance_enricher 需要复用 response_interceptor 的插件参数读取方式。
# 修改方式：继续使用同一个 ContextVar，由 apply_response_interceptors 和 apply_balance_enrichers 在调用前设置。
# 目的：让插件在响应处理和余额补充两个阶段都能通过 get_current_plugin_options() 读取自身参数。
_current_enabled_plugins: ContextVar[Optional[List[str]]] = ContextVar('_current_enabled_plugins', default=None)

def parse_plugin_entry(entry: str) -> Tuple[str, Optional[str]]:
    """
    解析单个插件条目，分离插件名和参数
    
    格式：plugin_name 或 plugin_name:options
    
    Args:
        entry: 插件条目字符串，如 "gthink:max" 或 "claude_thinking"
        
    Returns:
        (plugin_name, options) 元组，options 可能为 None
        
    Examples:
        >>> parse_plugin_entry("gthink:max")
        ("gthink", "max")
        >>> parse_plugin_entry("gthink:12000")
        ("gthink", "12000")
        >>> parse_plugin_entry("claude_thinking")
        ("claude_thinking", None)
        >>> parse_plugin_entry("my_plugin:foo,bar")
        ("my_plugin", "foo,bar")
    """
    if not entry or not isinstance(entry, str):
        return ("", None)
    
    entry = entry.strip()
    if not entry:
        return ("", None)
    
    # 查找第一个冒号
    colon_idx = entry.find(":")
    if colon_idx == -1:
        # 没有冒号，整个是插件名
        return (entry, None)
    
    plugin_name = entry[:colon_idx].strip()
    options = entry[colon_idx + 1:].strip()
    
    return (plugin_name, options if options else None)


def parse_enabled_plugins(enabled_plugins: Optional[List[str]]) -> Dict[str, Optional[str]]:
    """
    解析 enabled_plugins 列表，返回 {plugin_name: options} 映射
    
    Args:
        enabled_plugins: 插件列表，如 ["gthink:max", "claude_thinking", "my_plugin:foo,bar"]
        
    Returns:
        {plugin_name: options} 字典，options 可能为 None
        
    Examples:
        >>> parse_enabled_plugins(["gthink:max", "claude_thinking"])
        {"gthink": "max", "claude_thinking": None}
    """
    if not enabled_plugins or not isinstance(enabled_plugins, list):
        return {}
    
    result = {}
    for entry in enabled_plugins:
        plugin_name, options = parse_plugin_entry(entry)
        if plugin_name:
            result[plugin_name] = options
    
    return result


def get_plugin_options(plugin_name: str, provider: Dict[str, Any]) -> Optional[str]:
    """
    从 provider 配置中获取指定插件的参数
    
    这是插件内部读取参数的推荐方式。
    
    Args:
        plugin_name: 插件名称
        provider: 提供商配置（包含 preferences.enabled_plugins）
        
    Returns:
        插件参数字符串，如果没有参数则返回 None
        
    Examples:
        # provider = {"preferences": {"enabled_plugins": ["gthink:max", "claude_thinking"]}}
        >>> get_plugin_options("gthink", provider)
        "max"
        >>> get_plugin_options("claude_thinking", provider)
        None
        >>> get_plugin_options("not_enabled", provider)
        None
    """
    prefs = provider.get("preferences") if isinstance(provider, dict) else None
    if not prefs or not isinstance(prefs, dict):
        return None
    
    enabled_plugins = prefs.get("enabled_plugins")
    if not enabled_plugins or not isinstance(enabled_plugins, list):
        return None
    
    plugin_options = parse_enabled_plugins(enabled_plugins)
    return plugin_options.get(plugin_name)


def is_plugin_enabled(plugin_name: str, provider: Dict[str, Any]) -> bool:
    """
    检查指定插件是否在 provider 中启用
    
    Args:
        plugin_name: 插件名称
        provider: 提供商配置
        
    Returns:
        是否启用
    """
    prefs = provider.get("preferences") if isinstance(provider, dict) else None
    if not prefs or not isinstance(prefs, dict):
        return False
    
    enabled_plugins = prefs.get("enabled_plugins")
    if not enabled_plugins or not isinstance(enabled_plugins, list):
        return False
    
    plugin_options = parse_enabled_plugins(enabled_plugins)
    return plugin_name in plugin_options


def get_current_plugin_options(plugin_name: str) -> Optional[str]:
    """在响应拦截器回调内部读取当前插件的参数。

    该函数利用 ContextVar 获取当前调用链的 enabled_plugins，
    从中解析出指定插件的 options 字符串。

    仅在 apply_response_interceptors 或 apply_balance_enrichers 调用回调期间有效，
    其他时刻调用返回 None。

    Args:
        plugin_name: 插件名称

    Returns:
        插件参数字符串，未启用或无参数时返回 None
    """
    enabled_plugins = _current_enabled_plugins.get()
    if not enabled_plugins:
        return None
    plugin_map = parse_enabled_plugins(enabled_plugins)
    return plugin_map.get(plugin_name)


# 类型定义
# RequestInterceptor: (request, engine, provider, api_key, url, headers, payload) -> (url, headers, payload)
RequestInterceptor = Callable[
    [Any, str, Dict[str, Any], Optional[str], str, Dict[str, Any], Dict[str, Any]],
    "asyncio.coroutines.coroutine"
]

# ResponseInterceptor: (response_chunk, engine, model, is_stream) -> response_chunk
ResponseInterceptor = Callable[
    [Any, str, str, bool],
    "asyncio.coroutines.coroutine"
]

# BalanceEnricher: (result, engine, provider) -> result
# 修改原因：余额查询结果需要独立于 oai_tools 等请求插件做被动信息补充。
# 修改方式：新增专用回调类型，接收余额 result、engine 和 provider，并返回补充后的 result。
# 目的：让插件可以为 balance 结果添加 tier、rpm、tpm 等字段，而不改动 core.balance 模板。
BalanceEnricher = Callable[
    [Dict[str, Any], str, Dict[str, Any]],
    "asyncio.coroutines.coroutine"
]


@dataclass
class InterceptorEntry:
    """拦截器条目"""
    id: str
    callback: Callable
    priority: int = 100
    enabled: bool = True
    plugin_name: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ==================== 入站拦截器类型定义 ====================

# 入站拦截器回调签名:
#   async def interceptor(request_data, request, api_key_info, enabled_plugins) -> request_data
# 参数:
#   request_data: RequestModel 或其他请求数据对象
#   request: 原始 FastAPI Request 对象
#   api_key_info: dict，包含 api_key, api_index, model 等鉴权后的信息
#   enabled_plugins: 该 key 启用的插件列表
# 返回:
#   修改后的 request_data（或原样返回）
InboundInterceptor = Callable[..., Any]


class InterceptorRegistry:
    """
    拦截器注册表
    
    管理 inbound、request、response 拦截器和 balance_enricher 的注册、注销和调用。
    """
    
    def __init__(self):
        self._inbound_interceptors: Dict[str, InterceptorEntry] = {}
        self._request_interceptors: Dict[str, InterceptorEntry] = {}
        self._response_interceptors: Dict[str, InterceptorEntry] = {}
        # 修改原因：余额查询结果需要独立的后处理扩展点，不能混入 request/response interceptor。
        # 修改方式：为 balance_enricher 单独维护注册表，仍复用 InterceptorEntry 的优先级、启用状态和插件名字段。
        # 目的：让 oai_tier 等插件可以只补充 balance result，而不影响请求发送或响应内容。
        self._balance_enrichers: Dict[str, InterceptorEntry] = {}
    
    # ==================== 入站拦截器 ====================
    
    def register_inbound_interceptor(
        self,
        interceptor_id: str,
        callback: InboundInterceptor,
        priority: int = 100,
        plugin_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> InterceptorEntry:
        """
        注册入站拦截器
        
        入站拦截器在请求进入 handler 后、分配给上游 provider 之前执行。
        此时鉴权已完成，模型名已解析，但还没有选择 provider 或进入 channel。
        
        Args:
            interceptor_id: 拦截器唯一标识
            callback: 拦截器回调函数，签名为:
                async def interceptor(request_data, request, api_key_info, enabled_plugins)
                    -> request_data
            priority: 优先级（数值越小越先执行，默认 100）
            plugin_name: 所属插件名称
            metadata: 元数据
            overwrite: 是否覆盖已存在的拦截器
        """
        if interceptor_id in self._inbound_interceptors and not overwrite:
            raise ValueError(f"Inbound interceptor '{interceptor_id}' already registered")
        
        entry = InterceptorEntry(
            id=interceptor_id,
            callback=callback,
            priority=priority,
            plugin_name=plugin_name,
            metadata=metadata or {},
        )
        self._inbound_interceptors[interceptor_id] = entry
        logger.debug(f"Registered inbound interceptor: {interceptor_id} (priority={priority})")
        return entry
    
    def unregister_inbound_interceptor(self, interceptor_id: str) -> bool:
        """注销入站拦截器"""
        if interceptor_id in self._inbound_interceptors:
            del self._inbound_interceptors[interceptor_id]
            logger.debug(f"Unregistered inbound interceptor: {interceptor_id}")
            return True
        return False
    
    def get_inbound_interceptors(self, enabled_only: bool = True) -> List[InterceptorEntry]:
        """获取所有入站拦截器（按优先级排序）"""
        interceptors = list(self._inbound_interceptors.values())
        if enabled_only:
            interceptors = [i for i in interceptors if i.enabled]
        interceptors.sort(key=lambda i: i.priority)
        return interceptors
    
    async def apply_inbound_interceptors(
        self,
        request_data: Any,
        request: Any,
        api_key_info: Dict[str, Any],
        enabled_plugins: Optional[List[str]] = None,
    ) -> Any:
        """
        应用所有入站拦截器
        
        在 handler.request_model 入口处调用，鉴权完成后、provider 选择前。
        拦截器可以修改 request_data（如剥字段、注入参数等）。
        
        Args:
            request_data: 请求数据对象（RequestModel 等）
            request: 原始 FastAPI Request 对象
            api_key_info: 鉴权后的信息 dict
            enabled_plugins: 该 key 启用的插件列表
            
        Returns:
            经过所有拦截器处理后的 request_data
        """
        interceptors = self.get_inbound_interceptors(enabled_only=True)
        
        enabled_plugin_names = None
        if enabled_plugins is not None:
            enabled_plugin_names = set(parse_enabled_plugins(enabled_plugins).keys())
        
        for interceptor in interceptors:
            if interceptor.plugin_name:
                if not enabled_plugin_names or interceptor.plugin_name not in enabled_plugin_names:
                    continue
            
            try:
                result = await interceptor.callback(request_data, request, api_key_info, enabled_plugins)
                if result is not None:
                    request_data = result
            except Exception as e:
                logger.error(f"Inbound interceptor '{interceptor.id}' error: {e}")
        
        return request_data
    
    # ==================== 请求拦截器 ====================
    
    def register_request_interceptor(
        self,
        interceptor_id: str,
        callback: RequestInterceptor,
        priority: int = 100,
        plugin_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> InterceptorEntry:
        """
        注册请求拦截器
        
        Args:
            interceptor_id: 拦截器唯一标识
            callback: 拦截器回调函数，签名为:
                async def interceptor(request, engine, provider, api_key, url, headers, payload)
                    -> (url, headers, payload)
            priority: 优先级（数值越小越先执行，默认 100）
            plugin_name: 所属插件名称
            metadata: 元数据
            overwrite: 是否覆盖已存在的拦截器
            
        Returns:
            注册的 InterceptorEntry 对象
            
        Raises:
            ValueError: 如果拦截器已存在且 overwrite=False
        """
        if interceptor_id in self._request_interceptors and not overwrite:
            raise ValueError(f"Request interceptor '{interceptor_id}' already registered")
        
        entry = InterceptorEntry(
            id=interceptor_id,
            callback=callback,
            priority=priority,
            plugin_name=plugin_name,
            metadata=metadata or {},
        )
        self._request_interceptors[interceptor_id] = entry
        logger.debug(f"Registered request interceptor: {interceptor_id} (priority={priority})")
        return entry
    
    def unregister_request_interceptor(self, interceptor_id: str) -> bool:
        """注销请求拦截器"""
        if interceptor_id in self._request_interceptors:
            del self._request_interceptors[interceptor_id]
            logger.debug(f"Unregistered request interceptor: {interceptor_id}")
            return True
        return False
    
    def get_request_interceptors(self, enabled_only: bool = True) -> List[InterceptorEntry]:
        """获取所有请求拦截器（按优先级排序）"""
        interceptors = list(self._request_interceptors.values())
        if enabled_only:
            interceptors = [i for i in interceptors if i.enabled]
        interceptors.sort(key=lambda i: i.priority)
        return interceptors
    
    async def apply_request_interceptors(
        self,
        request: Any,
        engine: str,
        provider: Dict[str, Any],
        api_key: Optional[str],
        url: str,
        headers: Dict[str, Any],
        payload: Dict[str, Any],
        enabled_plugins: Optional[List[str]] = None,
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """
        应用所有请求拦截器
        
        按优先级顺序依次调用每个拦截器，每个拦截器可以修改 url/headers/payload。
        
        支持插件参数：enabled_plugins 中的条目可以是 "plugin_name:options" 格式，
        插件内部通过 get_plugin_options(plugin_name, provider) 读取参数。
        
        Args:
            request: 原始请求对象
            engine: 引擎类型
            provider: 提供商配置
            api_key: API 密钥
            url: 请求 URL
            headers: 请求头
            payload: 请求体
            enabled_plugins: 该渠道启用的插件列表，支持 "plugin:options" 格式
                            （None 表示不过滤，执行所有启用的拦截器）
            
        Returns:
            (url, headers, payload) 经过所有拦截器处理后的结果
        """
        interceptors = self.get_request_interceptors(enabled_only=True)
        
        # 解析 enabled_plugins，提取插件名（忽略参数部分用于过滤）
        enabled_plugin_names = None
        if enabled_plugins is not None:
            enabled_plugin_names = set(parse_enabled_plugins(enabled_plugins).keys())
        
        for interceptor in interceptors:
            # 如果拦截器属于某个插件，需要该插件被显式启用
            # enabled_plugins 为 None 或空列表时，跳过所有插件拦截器
            if interceptor.plugin_name:
                if not enabled_plugin_names or interceptor.plugin_name not in enabled_plugin_names:
                    continue
            
            try:
                result = await interceptor.callback(request, engine, provider, api_key, url, headers, payload)
                if result is not None:
                    if isinstance(result, tuple) and len(result) == 3:
                        url, headers, payload = result
                    else:
                        logger.warning(f"Request interceptor '{interceptor.id}' returned invalid result, expected (url, headers, payload)")
            except Exception as e:
                logger.error(f"Request interceptor '{interceptor.id}' error: {e}")
                # 继续执行其他拦截器
        
        return url, headers, payload
    
    # ==================== 响应拦截器 ====================
    
    def register_response_interceptor(
        self,
        interceptor_id: str,
        callback: ResponseInterceptor,
        priority: int = 100,
        plugin_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> InterceptorEntry:
        """
        注册响应拦截器
        
        Args:
            interceptor_id: 拦截器唯一标识
            callback: 拦截器回调函数，签名为:
                async def interceptor(response_chunk, engine, model, is_stream) -> response_chunk
            priority: 优先级（数值越小越先执行，默认 100）
            plugin_name: 所属插件名称
            metadata: 元数据
            overwrite: 是否覆盖已存在的拦截器
            
        Returns:
            注册的 InterceptorEntry 对象
        """
        if interceptor_id in self._response_interceptors and not overwrite:
            raise ValueError(f"Response interceptor '{interceptor_id}' already registered")
        
        entry = InterceptorEntry(
            id=interceptor_id,
            callback=callback,
            priority=priority,
            plugin_name=plugin_name,
            metadata=metadata or {},
        )
        self._response_interceptors[interceptor_id] = entry
        logger.debug(f"Registered response interceptor: {interceptor_id} (priority={priority})")
        return entry
    
    def unregister_response_interceptor(self, interceptor_id: str) -> bool:
        """注销响应拦截器"""
        if interceptor_id in self._response_interceptors:
            del self._response_interceptors[interceptor_id]
            logger.debug(f"Unregistered response interceptor: {interceptor_id}")
            return True
        return False
    
    def get_response_interceptors(self, enabled_only: bool = True) -> List[InterceptorEntry]:
        """获取所有响应拦截器（按优先级排序）"""
        interceptors = list(self._response_interceptors.values())
        if enabled_only:
            interceptors = [i for i in interceptors if i.enabled]
        interceptors.sort(key=lambda i: i.priority)
        return interceptors
    
    async def apply_response_interceptors(
        self,
        response_chunk: Any,
        engine: str,
        model: str,
        is_stream: bool,
        enabled_plugins: Optional[List[str]] = None,
    ) -> Any:
        """
        应用所有响应拦截器
        
        按优先级顺序依次调用每个拦截器，每个拦截器可以修改响应内容。
        
        enabled_plugins 会通过 ContextVar 暴露给回调，插件可通过
        get_current_plugin_options(plugin_name) 读取自己的参数。
        
        Args:
            response_chunk: 响应数据（流式时为单个 chunk，非流式时为完整响应）
            engine: 引擎类型
            model: 模型名称
            is_stream: 是否为流式响应
            enabled_plugins: 该渠道启用的插件列表，支持 "plugin:options" 格式
                            （None 表示不过滤，执行所有启用的拦截器）
            
        Returns:
            经过所有拦截器处理后的响应数据
        """
        interceptors = self.get_response_interceptors(enabled_only=True)
        
        # 将 enabled_plugins 写入 ContextVar，供回调通过 get_current_plugin_options() 读取
        token = _current_enabled_plugins.set(enabled_plugins)
        
        # 解析 enabled_plugins，提取插件名（忽略参数部分用于过滤）
        enabled_plugin_names = None
        if enabled_plugins is not None:
            enabled_plugin_names = set(parse_enabled_plugins(enabled_plugins).keys())
        
        for interceptor in interceptors:
            # 如果拦截器属于某个插件，需要该插件被显式启用
            # enabled_plugins 为 None 或空列表时，跳过所有插件拦截器
            if interceptor.plugin_name:
                if not enabled_plugin_names or interceptor.plugin_name not in enabled_plugin_names:
                    continue
            
            try:
                result = await interceptor.callback(response_chunk, engine, model, is_stream)
                if result is not None:
                    response_chunk = result
            except Exception as e:
                logger.error(f"Response interceptor '{interceptor.id}' error: {e}")
                # 继续执行其他拦截器
        
        # 恢复 ContextVar
        _current_enabled_plugins.reset(token)
        
        return response_chunk
    
    # ==================== 余额补充器 ====================
    
    def register_balance_enricher(
        self,
        enricher_id: str,
        callback: BalanceEnricher,
        priority: int = 100,
        plugin_name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
    ) -> InterceptorEntry:
        """
        注册余额补充器
        
        Args:
            enricher_id: 余额补充器唯一标识
            callback: 余额补充器回调函数，签名为:
                async def enricher(result, engine, provider) -> result
            priority: 优先级（数值越小越先执行，默认 100）
            plugin_name: 所属插件名称
            metadata: 元数据
            overwrite: 是否覆盖已存在的补充器
            
        Returns:
            注册的 InterceptorEntry 对象
        """
        if enricher_id in self._balance_enrichers and not overwrite:
            raise ValueError(f"Balance enricher '{enricher_id}' already registered")
        
        entry = InterceptorEntry(
            id=enricher_id,
            callback=callback,
            priority=priority,
            plugin_name=plugin_name,
            metadata=metadata or {},
        )
        self._balance_enrichers[enricher_id] = entry
        logger.debug(f"Registered balance enricher: {enricher_id} (priority={priority})")
        return entry
    
    def unregister_balance_enricher(self, enricher_id: str) -> bool:
        """注销余额补充器"""
        if enricher_id in self._balance_enrichers:
            del self._balance_enrichers[enricher_id]
            logger.debug(f"Unregistered balance enricher: {enricher_id}")
            return True
        return False
    
    def get_balance_enrichers(self, enabled_only: bool = True) -> List[InterceptorEntry]:
        """获取所有余额补充器（按优先级排序）"""
        enrichers = list(self._balance_enrichers.values())
        if enabled_only:
            enrichers = [i for i in enrichers if i.enabled]
        enrichers.sort(key=lambda i: i.priority)
        return enrichers
    
    async def apply_balance_enrichers(
        self,
        result: Dict[str, Any],
        engine: str,
        provider: Dict[str, Any],
        enabled_plugins: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        应用所有余额补充器
        
        按优先级顺序依次调用每个补充器，每个补充器可以修改 balance result。
        
        enabled_plugins 会通过 ContextVar 暴露给回调，插件可通过
        get_current_plugin_options(plugin_name) 读取自己的参数。
        
        Args:
            result: 余额查询返回的结果字典
            engine: 引擎类型
            provider: 提供商配置
            enabled_plugins: 该渠道启用的插件列表，支持 "plugin:options" 格式
                            （None 表示不过滤，执行所有启用的补充器）
            
        Returns:
            经过所有补充器处理后的余额结果字典
        """
        enrichers = self.get_balance_enrichers(enabled_only=True)
        if result is None:
            result = {}
        
        # 修改原因：balance_enricher 需要与 response_interceptor 一样按 enabled_plugins 过滤并支持参数读取。
        # 修改方式：调用前写入 ContextVar，并把 enabled_plugins 解析成插件名集合用于筛选 plugin_name。
        # 目的：只有渠道显式启用的插件才能补充余额结果，同时保留 plugin:options 的兼容格式。
        token = _current_enabled_plugins.set(enabled_plugins)
        enabled_plugin_names = None
        if enabled_plugins is not None:
            enabled_plugin_names = set(parse_enabled_plugins(enabled_plugins).keys())
        
        try:
            for enricher in enrichers:
                if enricher.plugin_name:
                    if not enabled_plugin_names or enricher.plugin_name not in enabled_plugin_names:
                        continue
                
                try:
                    enriched = await enricher.callback(result, engine, provider)
                    if enriched is not None:
                        if isinstance(enriched, dict):
                            result = enriched
                        else:
                            logger.warning(f"Balance enricher '{enricher.id}' returned invalid result, expected dict")
                except Exception as e:
                    logger.error(f"Balance enricher '{enricher.id}' error: {e}")
                    # 继续执行其他补充器
        finally:
            _current_enabled_plugins.reset(token)
        
        return result
    
    # ==================== 启用/禁用 ====================
    
    def enable_request_interceptor(self, interceptor_id: str) -> bool:
        """启用请求拦截器"""
        if interceptor_id in self._request_interceptors:
            self._request_interceptors[interceptor_id].enabled = True
            return True
        return False
    
    def disable_request_interceptor(self, interceptor_id: str) -> bool:
        """禁用请求拦截器"""
        if interceptor_id in self._request_interceptors:
            self._request_interceptors[interceptor_id].enabled = False
            return True
        return False
    
    def enable_response_interceptor(self, interceptor_id: str) -> bool:
        """启用响应拦截器"""
        if interceptor_id in self._response_interceptors:
            self._response_interceptors[interceptor_id].enabled = True
            return True
        return False
    
    def disable_response_interceptor(self, interceptor_id: str) -> bool:
        """禁用响应拦截器"""
        if interceptor_id in self._response_interceptors:
            self._response_interceptors[interceptor_id].enabled = False
            return True
        return False
    
    # ==================== 按插件注销 ====================
    
    def unregister_plugin_interceptors(self, plugin_name: str) -> int:
        """
        注销指定插件的所有拦截器
        
        Args:
            plugin_name: 插件名称
            
        Returns:
            注销的拦截器数量
        """
        count = 0
        
        # 注销请求拦截器
        to_remove = [i.id for i in self._request_interceptors.values() if i.plugin_name == plugin_name]
        for interceptor_id in to_remove:
            del self._request_interceptors[interceptor_id]
            count += 1
        
        # 注销响应拦截器
        to_remove = [i.id for i in self._response_interceptors.values() if i.plugin_name == plugin_name]
        for interceptor_id in to_remove:
            del self._response_interceptors[interceptor_id]
            count += 1
        
        # 修改原因：插件卸载时如果只清理 request/response，会留下余额补充器继续污染余额结果。
        # 修改方式：按 plugin_name 同步删除 balance_enricher 注册项，并计入注销数量。
        # 目的：保证 oai_tier 等插件卸载后不会继续向 balance result 注入字段。
        to_remove = [i.id for i in self._balance_enrichers.values() if i.plugin_name == plugin_name]
        for enricher_id in to_remove:
            del self._balance_enrichers[enricher_id]
            count += 1
        
        if count > 0:
            logger.debug(f"Unregistered {count} interceptors for plugin: {plugin_name}")
        
        return count
    
    # ==================== 状态查询 ====================
    
    def get_stats(self) -> Dict[str, Any]:
        """获取拦截器统计信息"""
        return {
            "request_interceptors": {
                "total": len(self._request_interceptors),
                "enabled": len([i for i in self._request_interceptors.values() if i.enabled]),
                "interceptors": [
                    {
                        "id": i.id,
                        "priority": i.priority,
                        "enabled": i.enabled,
                        "plugin_name": i.plugin_name,
                    }
                    for i in sorted(self._request_interceptors.values(), key=lambda x: x.priority)
                ],
            },
            "response_interceptors": {
                "total": len(self._response_interceptors),
                "enabled": len([i for i in self._response_interceptors.values() if i.enabled]),
                "interceptors": [
                    {
                        "id": i.id,
                        "priority": i.priority,
                        "enabled": i.enabled,
                        "plugin_name": i.plugin_name,
                    }
                    for i in sorted(self._response_interceptors.values(), key=lambda x: x.priority)
                ],
            },
            # 修改原因：新增 balance_enricher 后，管理端和测试需要看到该扩展点的注册状态。
            # 修改方式：在 get_stats 中加入 total、enabled 和按优先级排序的 enrichers 列表。
            # 目的：便于排查 oai_tier 是否已注册，并与 request/response 统计保持一致。
            "balance_enrichers": {
                "total": len(self._balance_enrichers),
                "enabled": len([i for i in self._balance_enrichers.values() if i.enabled]),
                "enrichers": [
                    {
                        "id": i.id,
                        "priority": i.priority,
                        "enabled": i.enabled,
                        "plugin_name": i.plugin_name,
                    }
                    for i in sorted(self._balance_enrichers.values(), key=lambda x: x.priority)
                ],
            },
        }
    
    def get_interceptor_plugins(self) -> List[Dict[str, Any]]:
        """
        获取所有注册了拦截器的插件列表
        
        Returns:
            插件信息列表，每个元素包含 plugin_name 和该插件注册的拦截器信息
        """
        plugins = {}
        
        # 收集请求拦截器的插件
        for interceptor in self._request_interceptors.values():
            if interceptor.plugin_name:
                if interceptor.plugin_name not in plugins:
                    plugins[interceptor.plugin_name] = {
                        "plugin_name": interceptor.plugin_name,
                        "request_interceptors": [],
                        "response_interceptors": [],
                        "balance_enrichers": [],
                    }
                plugins[interceptor.plugin_name]["request_interceptors"].append({
                    "id": interceptor.id,
                    "priority": interceptor.priority,
                    "enabled": interceptor.enabled,
                })
        
        # 收集响应拦截器的插件
        for interceptor in self._response_interceptors.values():
            if interceptor.plugin_name:
                if interceptor.plugin_name not in plugins:
                    plugins[interceptor.plugin_name] = {
                        "plugin_name": interceptor.plugin_name,
                        "request_interceptors": [],
                        "response_interceptors": [],
                        "balance_enrichers": [],
                    }
                plugins[interceptor.plugin_name]["response_interceptors"].append({
                    "id": interceptor.id,
                    "priority": interceptor.priority,
                    "enabled": interceptor.enabled,
                })
        
        # 修改原因：只注册 balance_enricher 的插件也应能出现在拦截器插件列表中。
        # 修改方式：按 plugin_name 收集余额补充器，并写入 balance_enrichers 数组。
        # 目的：让插件状态查询能完整展示 oai_tier 的响应拦截器和余额补充器。
        for enricher in self._balance_enrichers.values():
            if enricher.plugin_name:
                if enricher.plugin_name not in plugins:
                    plugins[enricher.plugin_name] = {
                        "plugin_name": enricher.plugin_name,
                        "request_interceptors": [],
                        "response_interceptors": [],
                        "balance_enrichers": [],
                    }
                plugins[enricher.plugin_name]["balance_enrichers"].append({
                    "id": enricher.id,
                    "priority": enricher.priority,
                    "enabled": enricher.enabled,
                })
        
        return list(plugins.values())
    
    def clear(self) -> None:
        """清空所有拦截器"""
        self._request_interceptors.clear()
        self._response_interceptors.clear()
        # 修改原因：测试和插件重载调用 clear 时也必须移除余额补充器。
        # 修改方式：同步清空 _balance_enrichers。
        # 目的：避免旧 balance_enricher 在全局注册表重用时残留。
        self._balance_enrichers.clear()


# 全局拦截器注册表实例
_interceptor_registry: Optional[InterceptorRegistry] = None


def get_interceptor_registry() -> InterceptorRegistry:
    """获取全局拦截器注册表实例"""
    global _interceptor_registry
    if _interceptor_registry is None:
        _interceptor_registry = InterceptorRegistry()
    return _interceptor_registry


def reset_interceptor_registry() -> None:
    """重置全局拦截器注册表（主要用于测试）"""
    global _interceptor_registry
    _interceptor_registry = None


# ==================== 便捷函数 ====================

def register_inbound_interceptor(
    interceptor_id: str,
    callback: InboundInterceptor,
    priority: int = 100,
    plugin_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
) -> InterceptorEntry:
    """注册入站拦截器（便捷函数）"""
    return get_interceptor_registry().register_inbound_interceptor(
        interceptor_id, callback, priority, plugin_name, metadata, overwrite
    )


def unregister_inbound_interceptor(interceptor_id: str) -> bool:
    """注销入站拦截器（便捷函数）"""
    return get_interceptor_registry().unregister_inbound_interceptor(interceptor_id)


async def apply_inbound_interceptors(
    request_data: Any,
    request: Any,
    api_key_info: Dict[str, Any],
    enabled_plugins: Optional[List[str]] = None,
) -> Any:
    """应用所有入站拦截器（便捷函数）"""
    return await get_interceptor_registry().apply_inbound_interceptors(
        request_data, request, api_key_info, enabled_plugins
    )


def register_request_interceptor(
    interceptor_id: str,
    callback: RequestInterceptor,
    priority: int = 100,
    plugin_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
) -> InterceptorEntry:
    """注册请求拦截器（便捷函数）"""
    return get_interceptor_registry().register_request_interceptor(
        interceptor_id, callback, priority, plugin_name, metadata, overwrite
    )


def unregister_request_interceptor(interceptor_id: str) -> bool:
    """注销请求拦截器（便捷函数）"""
    return get_interceptor_registry().unregister_request_interceptor(interceptor_id)


def register_response_interceptor(
    interceptor_id: str,
    callback: ResponseInterceptor,
    priority: int = 100,
    plugin_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
) -> InterceptorEntry:
    """注册响应拦截器（便捷函数）"""
    return get_interceptor_registry().register_response_interceptor(
        interceptor_id, callback, priority, plugin_name, metadata, overwrite
    )


def unregister_response_interceptor(interceptor_id: str) -> bool:
    """注销响应拦截器（便捷函数）"""
    return get_interceptor_registry().unregister_response_interceptor(interceptor_id)


def register_balance_enricher(
    enricher_id: str,
    callback: BalanceEnricher,
    priority: int = 100,
    plugin_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    overwrite: bool = False,
) -> InterceptorEntry:
    """注册余额补充器（便捷函数）"""
    # 修改原因：插件需要像注册 response_interceptor 一样注册 balance_enricher。
    # 修改方式：提供模块级便捷函数并转发到全局 InterceptorRegistry。
    # 目的：让 plugins/oai_tier.py 可以通过 core.plugins 统一导入并注册余额补充器。
    return get_interceptor_registry().register_balance_enricher(
        enricher_id, callback, priority, plugin_name, metadata, overwrite
    )


def unregister_balance_enricher(enricher_id: str) -> bool:
    """注销余额补充器（便捷函数）"""
    return get_interceptor_registry().unregister_balance_enricher(enricher_id)


async def apply_request_interceptors(
    request: Any,
    engine: str,
    provider: Dict[str, Any],
    api_key: Optional[str],
    url: str,
    headers: Dict[str, Any],
    payload: Dict[str, Any],
    enabled_plugins: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """应用所有请求拦截器（便捷函数）"""
    return await get_interceptor_registry().apply_request_interceptors(
        request, engine, provider, api_key, url, headers, payload, enabled_plugins
    )


async def apply_response_interceptors(
    response_chunk: Any,
    engine: str,
    model: str,
    is_stream: bool,
    enabled_plugins: Optional[List[str]] = None,
) -> Any:
    """应用所有响应拦截器（便捷函数）"""
    return await get_interceptor_registry().apply_response_interceptors(
        response_chunk, engine, model, is_stream, enabled_plugins
    )


async def apply_balance_enrichers(
    result: Dict[str, Any],
    engine: str,
    provider: Dict[str, Any],
    enabled_plugins: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """应用所有余额补充器（便捷函数）"""
    # 修改原因：余额路由需要在不感知具体插件的情况下补充 tier 等字段。
    # 修改方式：提供模块级 apply_balance_enrichers，并转发到全局注册表。
    # 目的：让 routes/channels.py 在 OAuth 和普通余额分支都能调用同一套补充链。
    return await get_interceptor_registry().apply_balance_enrichers(
        result, engine, provider, enabled_plugins
    )

# ==================== 透明 Client 包装 ====================

class InterceptedClient:
    """
    httpx.AsyncClient 的透明包装。

    在每次 HTTP 请求发出前，自动将 url 和 headers 传入请求拦截器链，
    让已启用的插件有机会修改请求头（如认证方式转换）。

    用于 models_adapter 等不经过 get_payload 的请求路径，
    使其也能被插件拦截，无需修改任何渠道代码。

    用法::

        from core.plugins.interceptors import InterceptedClient

        wrapped = InterceptedClient(client, engine, provider, enabled_plugins)
        # 之后将 wrapped 当作普通 httpx.AsyncClient 使用即可
    """

    def __init__(
        self,
        client,
        engine: str,
        provider: Dict[str, Any],
        enabled_plugins: Optional[List[str]] = None,
    ):
        self._client = client
        self._engine = engine
        # 修改原因：包装器只需要 provider 的当前快照，直接保存原始 dict 会延长请求级配置对象的生命周期。
        # 修改方式：浅拷贝 provider，并单独浅拷贝 preferences，避免继续强持有原始嵌套配置引用。
        # 目的：让请求结束或 close 后更快释放 provider、插件配置和底层 client。
        self._provider = dict(provider or {})
        preferences = self._provider.get("preferences")
        if isinstance(preferences, dict):
            self._provider["preferences"] = dict(preferences)
        self._enabled_plugins = list(enabled_plugins) if enabled_plugins else None
        api_key = self._provider.get("api", "")
        self._api_key = api_key[0] if isinstance(api_key, list) and api_key else (api_key or "")

    def close(self) -> None:
        """释放包装器持有的请求级引用。"""
        # 修改原因：InterceptedClient 会同时持有底层 client、engine、provider 和 enabled_plugins。
        # 修改方式：请求使用结束后显式置空这些属性，调用方在 finally 中执行 close。
        # 目的：断开包装器到请求级对象的强引用，降低 GC 处理引用链的压力。
        self._client = None
        self._engine = None
        self._provider = None
        self._enabled_plugins = None
        self._api_key = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    async def _intercept(self, url: str, headers: Optional[Dict] = None) -> Tuple[str, Dict]:
        """对 url 和 headers 应用请求拦截器"""
        if self._client is None:
            raise RuntimeError("InterceptedClient is closed")
        headers = dict(headers or {})
        if not self._enabled_plugins:
            return url, headers
        url, headers, _ = await apply_request_interceptors(
            None, self._engine, self._provider, self._api_key,
            str(url), headers, {},
            self._enabled_plugins,
        )
        return url, headers

    async def get(self, url, *, headers=None, **kwargs):
        url, headers = await self._intercept(url, headers)
        client = self._client
        if client is None:
            raise RuntimeError("InterceptedClient is closed")
        return await client.get(url, headers=headers, **kwargs)

    async def post(self, url, *, headers=None, **kwargs):
        url, headers = await self._intercept(url, headers)
        client = self._client
        if client is None:
            raise RuntimeError("InterceptedClient is closed")
        return await client.post(url, headers=headers, **kwargs)

    @asynccontextmanager
    async def _stream_intercepted(self, method, url, *, headers=None, **kwargs):
        url, headers = await self._intercept(url, headers)
        client = self._client
        if client is None:
            raise RuntimeError("InterceptedClient is closed")
        async with client.stream(method, url, headers=headers, **kwargs) as response:
            yield response

    def stream(self, method, url, *, headers=None, **kwargs):
        return self._stream_intercepted(method, url, headers=headers, **kwargs)

    def __getattr__(self, name):
        """未覆盖的属性和方法直接转发到原始 client"""
        client = self._client
        if client is None:
            raise AttributeError(f"InterceptedClient is closed; cannot access {name}")
        return getattr(client, name)
