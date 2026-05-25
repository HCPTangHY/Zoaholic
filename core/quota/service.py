from typing import Optional

from core.log_config import logger
from .types import QuotaSnapshot
from .normalizers import from_balance_result, from_oauth_account


class QuotaService:
    """统一额度查询服务。"""

    # 修改原因：额度查询需要逐步从路由中的分散逻辑迁移到统一服务层，但 P0 不接入 main.py。
    # 修改方式：服务构造函数只保存可选 app，并预留 enricher 列表供后续阶段接入。
    # 目的：先建立稳定查询入口，不影响当前生产路径。
    def __init__(self, app=None):
        self._app = app
        self._enrichers = []  # 后续 P1 用

    async def query(
        self,
        provider_config: dict,
        key_id: str,
        *,
        force: bool = False,
        engine: str = '',
    ) -> QuotaSnapshot:
        """统一查询入口。根据渠道类型走不同数据源，归一化后输出 QuotaSnapshot。"""
        from core.channels.registry import get_channel

        # 修改原因：调用方可能把 engine 放在参数里，也可能只放在 provider_config 中。
        # 修改方式：优先使用显式 engine，缺失时回退到 provider_config.engine/type。
        # 目的：让服务层既适配当前路由配置，也适配后续独立调用。
        channel_id = engine or provider_config.get('engine') or provider_config.get('type') or ''
        channel_def = get_channel(channel_id)
        is_oauth = channel_def.is_oauth if channel_def else False

        try:
            if is_oauth:
                return await self._query_oauth(provider_config, key_id, engine=channel_id, force=force)
            return await self._query_balance(provider_config, key_id, engine=channel_id, force=force)
        except Exception as e:
            logger.error(f"[QuotaService] query failed: {e}")
            return QuotaSnapshot(supported=True, status='error', error=str(e))

    async def _query_balance(self, provider_config: dict, key_id: str, engine: str = '', force: bool = False) -> QuotaSnapshot:
        """普通 Key 走 core/balance.py。"""
        from core.balance import query_provider_balance

        # 修改原因：实际 query_provider_balance 签名是 (client, provider)，需要 app.state.client_manager 创建 HTTP client。
        # 修改方式：P0 服务层在 app 不可用时返回 unsupported；可用时复用现有 client_manager 和 proxy_context。
        # 目的：不改 core.balance.py 签名，也不在服务层私自创建不受项目管理的 httpx client。
        state = getattr(self._app, 'state', None)
        client_manager = getattr(state, 'client_manager', None)
        if client_manager is None:
            return QuotaSnapshot(supported=False, status='unsupported', error='HTTP client manager not available')

        provider = dict(provider_config)
        if key_id:
            provider['api'] = key_id
            provider['api_key'] = key_id

        prefs = provider.get('preferences') if isinstance(provider.get('preferences'), dict) else {}
        app_config = getattr(state, 'config', {})
        app_prefs = app_config.get('preferences', {}) if isinstance(app_config, dict) else {}
        proxy = prefs.get('proxy') or provider.get('proxy') or app_prefs.get('proxy')
        target_url = provider.get('base_url') or 'https://localhost'

        from core.http import proxy_context
        with proxy_context(proxy):
            async with client_manager.get_client(target_url, proxy) as client:
                result = await query_provider_balance(client, provider)

        snap = from_balance_result(result)
        return await self._apply_enrichers(snap, engine=engine, provider=provider)

    async def _query_oauth(self, provider_config: dict, key_id: str, engine: str = '', force: bool = False) -> QuotaSnapshot:
        """OAuth Key 走 OAuthManager.fetch_quota。"""
        # 修改原因：当前 OAuthManager 的实际方法名是 fetch_quota(channel_id, key_id, force)，不是 fetch_quota_for_key。
        # 修改方式：运行时检查 app.state.oauth_manager.fetch_quota 是否可调用，并按真实签名传入渠道名和 key_id。
        # 目的：服务层与现有 OAuth quota 缓存和刷新逻辑保持一致，不引入并行实现。
        state = getattr(self._app, 'state', None)
        oauth_manager = getattr(state, 'oauth_manager', None)
        fetch_quota = getattr(oauth_manager, 'fetch_quota', None)
        if not callable(fetch_quota):
            return QuotaSnapshot(supported=False, status='unsupported', error='OAuth manager fetch_quota not available')

        channel_id = str(provider_config.get('provider') or provider_config.get('name') or engine or '').strip()
        if not channel_id:
            return QuotaSnapshot(supported=False, status='unsupported', error='OAuth channel id not available')

        account = await fetch_quota(channel_id, key_id, force=force)
        if not account:
            return QuotaSnapshot(supported=True, status='unknown')

        snap = from_oauth_account(account)
        return await self._apply_enrichers(snap, engine=engine, provider=provider_config)

    async def _apply_enrichers(self, snap: QuotaSnapshot, *, engine: str = '', provider: Optional[dict] = None) -> QuotaSnapshot:
        """执行后续阶段注册的额度增强器。"""
        # 修改原因：P1 会把插件补充字段迁移到 QuotaSnapshot，但 P0 只需要预留顺序执行入口。
        # 修改方式：统一封装 enricher 调用，并捕获单个增强器异常。
        # 目的：避免一个增强器失败影响基础余额查询结果。
        for enricher in self._enrichers:
            try:
                snap = await enricher(snap, engine=engine, provider=provider)
            except Exception as e:
                logger.warning(f"[QuotaService] enricher failed: {e}")
        return snap

    def register_enricher(self, callback):
        """注册额度增强器（后续 P1 用）。"""
        # 修改原因：后续插件需要能在统一查询服务上补充 tier、rpm、tpm 等信息。
        # 修改方式：把 callback 追加到服务实例的 enricher 列表，按注册顺序执行。
        # 目的：保留扩展点，同时不改变 P0 的基础查询行为。
        self._enrichers.append(callback)

    def unregister_enricher(self, callback):
        # 修改原因：插件热重载或关闭时需要移除已注册的额度增强器。
        # 修改方式：按对象身份过滤 callback，避免误删同名但不同实例的增强器。
        # 目的：让后续动态插件生命周期可以安全管理服务层扩展点。
        self._enrichers = [e for e in self._enrichers if e is not callback]
