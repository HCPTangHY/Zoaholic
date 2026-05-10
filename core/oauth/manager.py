"""OAuth 凭据管理器。"""

import asyncio
import inspect
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from .state import STATE_PATH, load_state

logger = logging.getLogger(__name__)


class OAuthManager:
    """负责 key_id 到 access_token 的解析、刷新和持久化。"""

    UNMAPPED_CHANNEL = "_unmapped"

    def __init__(self, state_path: str = STATE_PATH):
        # 修改原因：OAuth 凭据需要按渠道隔离，同一邮箱可能同时存在于多个 provider name 下。
        # 修改方式：manager 的 _state 统一改为 {channel_id: {key_id: credential}}，锁也改为 channel:key 维度。
        # 目的：避免不同渠道共用邮箱 key 时互相覆盖，同时保持 handler 只解析 access_token 的职责边界。
        self._state = {}
        self._locks = {}
        self._providers = {}
        self._state_path = state_path
        self._pkce_store = {}
        # 修改原因：OAuthManager 生命周期长于 api.yaml 配置，provider 需要在调用时读取当前 app.state.config。
        # 修改方式：保存一个配置获取函数引用，而不是保存某次启动时的配置副本。
        # 目的：前端保存 token_url 后，下一次刷新或授权码交换能立即使用新配置。
        self._config_ref = None
        # 修改原因：Codex 被动额度采集会在普通请求路径频繁触发，不能每次更新都同步写磁盘。
        # 修改方式：用 generation 标记内存 quota 是否变更，并用 30 秒 call_later 定时器批量持久化。
        # 目的：让 quota 缓存能尽快服务前端，同时避免高频请求放大 oauth_state.json 写入。
        self._quota_update_generation = 0
        self._quota_persisted_generation = 0
        self._quota_persist_handle = None
        self._quota_flush_delay = 30

    def register_provider(self, type_name: str, provider):
        """注册 OAuth provider。渠道 register() 或插件 setup() 中调用。"""
        self._providers[type_name] = provider
        # 修改原因：部分调用路径仍会直接使用 provider，例如 authorize 路由需要读取 redirect_mode 和构建授权 URL。
        # 修改方式：如果 provider 支持 set_config_getter，就注入 manager.get_config 作为运行时配置读取入口。
        # 目的：即使调用方没有显式传 config，CodexProvider 也能解析到最新 token_url。
        if hasattr(provider, "set_config_getter"):
            provider.set_config_getter(self.get_config)

    def set_config_ref(self, config_getter):
        """设置配置获取函数。main.py lifespan 调用。"""
        # 修改原因：main.py 持有 app.state.config，而 OAuth 子模块不应反向导入 main.app 以避免循环导入。
        # 修改方式：由 lifespan 把 lambda: app.state.config or {} 注入到 manager。
        # 目的：让 OAuth provider 每次请求前都能通过 manager 获取当前运行时配置。
        self._config_ref = config_getter

    def get_config(self) -> dict:
        """获取当前运行时配置。"""
        # 修改原因：配置引用可能在测试、启动早期或异常状态下不可调用或返回非 dict。
        # 修改方式：调用前检查 callable，调用后只接受 dict，任何异常都回退为空配置。
        # 目的：保证 OAuth 刷新路径失败时最多回退默认 token endpoint，不因配置引用异常崩溃。
        if not callable(self._config_ref):
            return {}
        try:
            config = self._config_ref()
        except Exception:
            return {}
        return config if isinstance(config, dict) else {}

    async def init(self):
        """加载状态文件。provider 注册由各渠道/插件自行调用 register_provider 完成。"""
        loaded_state = load_state(self._state_path)
        # 修改原因：历史版本 oauth_state.json 是 {email: credential} 扁平结构，升级后必须在启动时改为分渠道结构。
        # 修改方式：检测第一层 value 是否为“账号字典”，旧结构按 credential.type 映射到 api.yaml 中第一个同 engine 渠道。
        # 目的：无需用户手工迁移，也能消除同邮箱跨渠道冲突；无法映射的旧账号进入 _unmapped 便于人工处理。
        if self._is_legacy_flat_state(loaded_state):
            self._state = self._migrate_legacy_state(loaded_state)
            await self._persist()
        else:
            self._state = self._normalize_nested_state(loaded_state)

    def _looks_like_credential(self, value: dict) -> bool:
        """判断 dict 是否像单个 credential，而不是渠道下的账号映射。"""
        # 修改原因：新旧 state 的第一层 value 都是 dict，仅靠类型无法区分。
        # 修改方式：检查 OAuth 凭据常见字段；命中时认为它是旧扁平结构中的账号凭据。
        # 目的：让启动迁移能准确识别旧文件，同时不误伤正常的 {channel: {email: cred}} 结构。
        credential_fields = {
            "type",
            "access_token",
            "refresh_token",
            "id_token",
            "expires_at",
            "email",
            "status",
        }
        return any(field in value for field in credential_fields)

    def _is_legacy_flat_state(self, state: dict) -> bool:
        """检测 oauth_state.json 是否仍是旧扁平结构。"""
        # 修改原因：空 state 无需迁移；非空 state 要区分旧的 email->cred 和新的 channel->accounts。
        # 修改方式：第一层 value 若不是 dict of dict，或直接带凭据字段，就判定为旧结构。
        # 目的：兼容旧文件、空文件和已经迁移过的新文件。
        if not isinstance(state, dict) or not state:
            return False
        for value in state.values():
            if not isinstance(value, dict):
                return True
            if self._looks_like_credential(value):
                return True
            if not all(isinstance(item, dict) for item in value.values()):
                return True
        return False

    def _normalize_nested_state(self, state: dict) -> dict:
        """把已是新结构的 state 清理为可安全访问的嵌套 dict。"""
        # 修改原因：手工编辑或历史异常可能留下非 dict 的渠道值或账号值。
        # 修改方式：只保留 dict 渠道和 dict 凭据，并把渠道名、账号名统一转成字符串 key。
        # 目的：后续读写不因脏数据抛出，同时不改变已经合法的新结构。
        normalized: dict[str, dict[str, dict]] = {}
        if not isinstance(state, dict):
            return normalized
        for channel_id, accounts in state.items():
            if not isinstance(accounts, dict):
                continue
            channel_key = self._normalize_channel_id(str(channel_id))
            normalized[channel_key] = {
                str(key_id): cred
                for key_id, cred in accounts.items()
                if isinstance(cred, dict)
            }
        return normalized

    def _engine_channel_map(self) -> dict[str, str]:
        """从当前配置中生成 engine -> 第一个 provider name 的映射。"""
        # 修改原因：旧扁平 state 只有 credential.type，没有渠道名，只能用 api.yaml 中 engine 相同的第一个渠道作为迁移目标。
        # 修改方式：遍历 runtime config.providers，按首次出现顺序记录 engine 到 provider/name 的映射。
        # 目的：符合用户要求的自动迁移规则，并在多个同 engine 渠道存在时保持确定性。
        config = self.get_config()
        providers = config.get("providers")
        if not isinstance(providers, list):
            api_config = config.get("api_config") if isinstance(config.get("api_config"), dict) else {}
            providers = api_config.get("providers") if isinstance(api_config.get("providers"), list) else []
        mapping: dict[str, str] = {}
        for item in providers:
            if not isinstance(item, dict):
                continue
            engine = str(item.get("engine") or item.get("type") or "").strip()
            provider_name = str(item.get("provider") or item.get("name") or "").strip()
            if engine and provider_name and engine not in mapping:
                mapping[engine] = provider_name
        return mapping

    def _migrate_legacy_state(self, state: dict) -> dict:
        """把旧 email->credential state 迁移为 channel->email->credential。"""
        # 修改原因：旧 state 第一层 key 是邮箱，和新结构第一层渠道名冲突，不能直接复用。
        # 修改方式：按 credential.type 查找 engine->provider 映射，找不到时放到 _unmapped。
        # 目的：最大限度保留已有凭据，并让无法自动判断渠道的账号可被管理员看见。
        engine_to_channel = self._engine_channel_map()
        migrated: dict[str, dict[str, dict]] = {}
        for key_id, cred in state.items():
            if not isinstance(cred, dict):
                continue
            type_name = str(cred.get("type") or "").strip()
            channel_id = engine_to_channel.get(type_name) or self.UNMAPPED_CHANNEL
            channel_id = self._normalize_channel_id(channel_id)
            migrated.setdefault(channel_id, {})[str(key_id)] = cred
        return migrated

    def _normalize_channel_id(self, channel_id: str | None) -> str:
        """统一渠道名空值处理。"""
        # 修改原因：所有 state 操作都以 provider name 为第一层 key，空字符串会造成难以定位的隐藏分组。
        # 修改方式：去掉首尾空白，空值统一落入 _unmapped。
        # 目的：迁移和异常调用都能被显式归档，避免写出空渠道名。
        normalized = str(channel_id or "").strip()
        return normalized or self.UNMAPPED_CHANNEL


    def _find_channel_config(self, channel_id: str) -> dict | None:
        """从运行时配置中找到指定渠道的 provider dict。"""
        config = self.get_config()
        providers = config.get("providers")
        if not isinstance(providers, list):
            api_config = config.get("api_config") if isinstance(config.get("api_config"), dict) else {}
            providers = api_config.get("providers") if isinstance(api_config.get("providers"), list) else []
        normalized = self._normalize_channel_id(channel_id)
        for p in providers:
            if not isinstance(p, dict):
                continue
            name = self._normalize_channel_id(str(p.get("provider") or p.get("name") or ""))
            if name == normalized:
                return p
        return None

    def _get_channel_accounts(self, channel_id: str | None, create: bool = False) -> dict:
        """获取某个渠道下的账号映射。"""
        # 修改原因：注册、解析、刷新、quota、重命名都需要同一套渠道字典访问规则。
        # 修改方式：集中处理渠道名规范化、缺失时是否创建、脏值是否重置。
        # 目的：避免每个公开方法重复访问 self._state 时遗漏嵌套层级。
        channel_key = self._normalize_channel_id(channel_id)
        accounts = self._state.get(channel_key)
        if isinstance(accounts, dict):
            return accounts
        if create:
            accounts = {}
            self._state[channel_key] = accounts
            return accounts
        return {}

    async def resolve(self, channel_id: str, key_id: str) -> str | None:
        """把指定渠道中的 key_id 解析成 access_token；不存在时返回 None。"""
        accounts = self._get_channel_accounts(channel_id)
        cred = accounts.get(key_id)
        if not cred:
            return None

        # 修改原因：连续 refresh 失败的账号会在请求路径中反复触发上游错误和本地落盘。
        # 修改方式：解析前检查 error_count 和最近失败时间，5 分钟熔断窗口内直接返回 None。
        # 目的：让 handler 可以切换其他 key，同时避免坏 refresh_token 放大失败影响。
        if self._is_refresh_circuit_open(cred):
            return None

        import time

        if time.time() > cred.get("expires_at", 0) - 300:
            async with self._get_lock(channel_id, key_id):
                accounts = self._get_channel_accounts(channel_id)
                cred = accounts.get(key_id)
                if not cred:
                    return None
                if self._is_refresh_circuit_open(cred):
                    return None
                if time.time() > cred.get("expires_at", 0) - 300:
                    try:
                        cred = await self._refresh(channel_id, key_id, cred)
                    except Exception as e:
                        # 修改原因：刷新失败若不写回状态，下一次请求会立刻重复同样的失败刷新。
                        # 修改方式：在对应渠道下累加 error_count，截断保存 last_error，并尽力持久化错误状态。
                        # 目的：让账号进入可观测的 error 状态，并让上层 handler 跳过当前 key。
                        error_count = cred.get("error_count", 0) + 1
                        cred["error_count"] = error_count
                        cred["last_error"] = str(e)[:500]
                        cred["last_error_at"] = datetime.utcnow().isoformat() + "Z"
                        cred["status"] = "error"
                        accounts[key_id] = cred
                        try:
                            await self._persist()
                        except Exception:
                            pass
                        logger.warning(
                            f"OAuth refresh failed for {channel_id}:{key_id} (attempt {error_count}): {e}"
                        )
                        return None
        return cred.get("access_token")

    def _is_refresh_circuit_open(self, cred: dict) -> bool:
        """判断账号是否处于 refresh 失败熔断窗口。"""
        # 修改原因：resolve 进入锁前后都需要同一套熔断判断，复制逻辑容易造成阈值不一致。
        # 修改方式：把“失败 5 次且最近 5 分钟内失败过”的判断集中到一个小函数。
        # 目的：保持请求前快速跳过坏账号，并兼顾并发刷新等待后的二次检查。
        error_count = cred.get("error_count", 0)
        if error_count < 5:
            return False
        last_error_at = cred.get("last_error_at", "")
        if not last_error_at:
            return False
        try:
            last_err_time = datetime.fromisoformat(last_error_at.replace("Z", "+00:00"))
            if last_err_time.tzinfo is None:
                last_err_time = last_err_time.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - last_err_time).total_seconds() < 300
        except (ValueError, TypeError):
            return False

    async def _refresh(self, channel_id: str, key_id: str, cred: dict) -> dict:
        """刷新指定渠道下的单个 OAuth 凭据并落盘。"""
        provider = self._providers.get(cred.get("type"))
        if not provider:
            raise ValueError(f"Unknown OAuth type: {cred.get('type')}")
        updated = await self._call_provider_method(provider.refresh_token, cred)
        updated["last_refresh"] = datetime.utcnow().isoformat() + "Z"
        updated["status"] = "active"
        updated["error_count"] = 0
        updated.setdefault("type", cred.get("type"))
        updated.setdefault("email", cred.get("email"))

        # 修改原因：OpenAI refresh token rotation 成功后，若落盘失败，磁盘旧 refresh_token 可能已失效。
        # 修改方式：写入新内存状态前保存旧值，_persist 抛错时恢复当前渠道下的旧状态或移除新增 key。
        # 目的：保证内存与磁盘在持久化失败时保持一致，避免运行期继续依赖未成功保存的新 token。
        accounts = self._get_channel_accounts(channel_id, create=True)
        old_cred = accounts.get(key_id)
        accounts[key_id] = updated
        try:
            await self._persist()
        except Exception:
            if old_cred is not None:
                accounts[key_id] = old_cred
            else:
                accounts.pop(key_id, None)
            raise
        return updated

    async def refresh_provider(self, type_name: str, credential: dict) -> dict:
        """调用指定 provider 刷新凭据，并传入当前配置。"""
        # 修改原因：routes.oauth 的手动导入路径也会直接刷新 token，不能绕过实时配置读取机制。
        # 修改方式：统一通过 _call_provider_method 调用 provider.refresh_token。
        # 目的：让后台自动刷新和手动导入刷新使用同一套 config 注入规则。
        provider = self._providers.get(type_name)
        if not provider:
            raise ValueError(f"Unknown OAuth type: {type_name}")
        return await self._call_provider_method(provider.refresh_token, credential)

    async def exchange_code(
        self,
        channel_id: str,
        type_name: str,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
    ) -> dict:
        """调用指定 provider 交换授权码，并传入当前配置。"""
        # 修改原因：OAuth 登录交换和 refresh 一样访问 token endpoint，也需要使用最新 token_url，同时路由要知道写入哪个渠道。
        # 修改方式：方法签名加入 channel_id 并规范化校验，实际 token 交换仍由 provider 完成，注册由路由调用 register(channel_id, ...)。
        # 目的：让 exchange 与后续注册的渠道上下文保持一致，避免授权成功后写入全局扁平 state。
        self._normalize_channel_id(channel_id)
        provider = self._providers.get(type_name)
        if not provider:
            raise ValueError(f"Unknown OAuth type: {type_name}")
        return await self._call_provider_method(
            provider.exchange_code,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )

    def _method_accepts_config(self, method) -> bool:
        """判断 provider 方法是否接受 config 参数。"""
        # 修改原因：历史测试替身和后续第三方 provider 可能仍使用旧签名，直接传 config 会导致 TypeError。
        # 修改方式：通过 inspect.signature 判断是否声明 config 或 **kwargs。
        # 目的：在不破坏旧 provider 的前提下，为新 provider 提供运行时配置。
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == "config" or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    async def _call_provider_method(self, method, *args, **kwargs):
        """调用 provider 方法，并在其支持时注入当前配置。"""
        # 修改原因：OAuthProvider 基类已支持可选 config，但旧 provider 或测试替身不一定同步更新签名。
        # 修改方式：只在目标方法声明 config 或 **kwargs 时添加 config=self.get_config()。
        # 目的：兼容旧实现，同时让 CodexProvider 每次调用都拿到最新 app.state.config。
        call_kwargs = dict(kwargs)
        if self._method_accepts_config(method) and "config" not in call_kwargs:
            call_kwargs["config"] = self.get_config()
        result = method(*args, **call_kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _get_cached_quota(self, cred: dict) -> dict | None:
        """从 oauth_state 凭据中读取已缓存 quota。"""
        # 修改原因：Codex 普通响应会被动写入 quota，前端按需查询时应优先使用缓存而不是再消耗一次上游请求。
        # 修改方式：只读取顶层 quota_5h、quota_7d 和 quota_raw，并把 quota_raw 映射回接口返回的 raw 字段。
        # 目的：保持 list_accounts 可直接展示百分比，同时让 /quota 返回结构与主动查询一致。
        if not isinstance(cred, dict):
            return None
        result = {}
        if cred.get("quota_5h") is not None:
            result["quota_5h"] = cred.get("quota_5h")
        if cred.get("quota_7d") is not None:
            result["quota_7d"] = cred.get("quota_7d")
        if isinstance(cred.get("quota_raw"), dict):
            result["raw"] = cred.get("quota_raw")
        return result if result else None

    async def fetch_quota(self, channel_id: str, key_id: str) -> dict | None:
        """获取指定渠道账号额度信息。"""
        # 修改原因：同一 key_id 可能存在于多个渠道，quota 查询必须只读取当前渠道下的凭据。
        # 修改方式：先从 channel_id 对应账号表查缓存；缓存不存在时，再通过 provider 主动查询并写回同渠道缓存。
        # 目的：减少上游额度探测请求，同时避免跨渠道读取到同邮箱的错误 quota。
        accounts = self._get_channel_accounts(channel_id)
        cred = accounts.get(key_id)
        if not cred:
            return None
        cached = self._get_cached_quota(cred)
        if cached:
            return cached
        provider = self._providers.get(cred.get("type"))
        if not provider or not hasattr(provider, "fetch_quota"):
            return None
        # 找当前渠道的 provider config（含 base_url 等），传给 provider 走反代
        channel_config = self._find_channel_config(channel_id)
        quota = await self._call_provider_method(provider.fetch_quota, cred, config=channel_config)
        if isinstance(quota, dict):
            self.update_quota(channel_id, key_id, quota)
            return quota
        return None

    def update_quota(self, channel_id: str, key_id: str, quota_data: dict) -> bool:
        """更新 OAuth 账号的 quota 内存缓存，并安排延迟落盘。"""
        # 修改原因：Codex 响应 wrapper 会在普通请求完成时拿到最新 x-ratelimit-*，这些数据要回写到对应渠道的账号 state。
        # 修改方式：按 channel_id/key_id 定位账号，只更新内存字段，使用 quota_raw 保存原始 header，并安排批量持久化。
        # 目的：让被动采集数据不串到其他同名账号，同时避免每次请求都写文件。
        if not isinstance(quota_data, dict):
            return False
        accounts = self._get_channel_accounts(channel_id)
        cred = accounts.get(key_id)
        if not isinstance(cred, dict):
            return False

        changed = False
        for field in ("quota_5h", "quota_7d"):
            if field in quota_data and quota_data.get(field) is not None:
                if cred.get(field) != quota_data.get(field):
                    cred[field] = quota_data.get(field)
                    changed = True
        if isinstance(quota_data.get("raw"), dict) and cred.get("quota_raw") != quota_data.get("raw"):
            cred["quota_raw"] = dict(quota_data["raw"])
            changed = True

        if not changed:
            return False
        cred["quota_updated_at"] = datetime.utcnow().isoformat() + "Z"
        self._quota_update_generation += 1
        self._schedule_quota_persist()
        return True

    def _schedule_quota_persist(self) -> None:
        """启动或复用 quota 延迟落盘定时器。"""
        # 修改原因：update_quota 是同步方法，可能从响应 adapter 中被频繁调用，不能直接 await 持久化。
        # 修改方式：在当前事件循环中用 call_later 安排一次 flush；没有运行事件循环时只保留 dirty 标记。
        # 目的：运行服务中自动批量落盘，单元测试或同步上下文中不制造悬挂任务。
        if self._quota_persist_handle is not None and not self._quota_persist_handle.cancelled():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def _run_flush():
            self._quota_persist_handle = None
            asyncio.create_task(self._flush_quota_updates())

        self._quota_persist_handle = loop.call_later(self._quota_flush_delay, _run_flush)

    async def _flush_quota_updates(self) -> None:
        """把 quota dirty generation 批量写入 oauth_state.json。"""
        # 修改原因：定时器触发时可能已经有多次 quota 更新，持久化只需要写一次完整 state。
        # 修改方式：记录本次 flush 覆盖的 generation，写盘完成后若期间又有新更新则重新安排下一次 flush。
        # 目的：保证最终落盘不丢更新，同时仍维持批量写入行为。
        generation = self._quota_update_generation
        if generation <= self._quota_persisted_generation:
            return
        await self._persist()
        self._quota_persisted_generation = max(self._quota_persisted_generation, generation)
        if self._quota_update_generation > generation:
            self._schedule_quota_persist()

    async def register(self, channel_id: str, key_id: str, type_name: str, token_data: dict):
        """注册或覆盖指定渠道下的 OAuth 账号。"""
        # 修改原因：手动导入和 OAuth 登录都必须写入当前 provider name 下，不能再以邮箱为全局 key。
        # 修改方式：复制传入 token_data，补齐 type、状态和错误计数后保存到 _state[channel_id][key_id]。
        # 目的：避免同邮箱在不同渠道中冲突，也让列表和解析逻辑获得统一字段。
        saved = dict(token_data or {})
        saved["type"] = type_name
        saved["status"] = "active"
        saved["error_count"] = 0
        accounts = self._get_channel_accounts(channel_id, create=True)
        accounts[key_id] = saved
        await self._persist()

    async def rename(self, channel_id: str, old_key_id: str, new_key_id: str):
        """重命名指定渠道内的 OAuth 账号标识符。"""
        # 修改原因：前端重命名 OAuth 标识符时，只应影响当前渠道，不能移动其他渠道的同名邮箱。
        # 修改方式：在 _state[channel_id] 内迁移 key，并把刷新锁从 channel:old_key 迁移到 channel:new_key。
        # 目的：让 api.yaml 中的新账号标识和 OAuth 运行时状态在同一渠道内保持一致。
        accounts = self._get_channel_accounts(channel_id)
        if old_key_id not in accounts:
            raise ValueError(f"Account not found: {old_key_id}")
        if new_key_id in accounts and new_key_id != old_key_id:
            raise ValueError(f"Account already exists: {new_key_id}")
        cred = accounts.pop(old_key_id)
        accounts[new_key_id] = cred
        old_lock_key = self._lock_key(channel_id, old_key_id)
        new_lock_key = self._lock_key(channel_id, new_key_id)
        if old_lock_key in self._locks:
            self._locks[new_lock_key] = self._locks.pop(old_lock_key)
        await self._persist()

    def _copy_account(self, cred: dict, include_tokens: bool) -> dict:
        """复制账号对象，并按调用场景决定是否脱敏 token。"""
        # 修改原因：普通列表接口不能泄露 token，但导出接口必须返回 refresh_token 才能用于备份恢复。
        # 修改方式：list_accounts 增加 include_tokens 开关；默认脱敏，导出端点显式打开。
        # 目的：同时满足日常管理安全性和管理员显式导出凭证的需求。
        copied = dict(cred)
        if not include_tokens:
            copied["access_token"] = "***"
            copied["refresh_token"] = "***"
        return copied

    def list_accounts(self, channel_id: str | None = None, include_tokens: bool = False) -> dict:
        """列出 OAuth 账号；默认隐藏 token 明文。"""
        # 修改原因：账号列表需要支持“全部渠道”和“单渠道”两种形态，而前端编辑页只需要当前渠道下的扁平账号表。
        # 修改方式：传 channel_id 时返回该渠道的 key_id->cred；不传时返回 channel_id->key_id->cred。
        # 目的：兼容管理端全量查看，同时让新前端按 provider 查询时得到旧 UI 可直接使用的数据形状。
        if channel_id is not None:
            accounts = self._get_channel_accounts(channel_id)
            return {
                key_id: self._copy_account(cred, include_tokens)
                for key_id, cred in accounts.items()
                if isinstance(cred, dict)
            }
        return {
            channel: {
                key_id: self._copy_account(cred, include_tokens)
                for key_id, cred in accounts.items()
                if isinstance(cred, dict)
            }
            for channel, accounts in self._state.items()
            if isinstance(accounts, dict)
        }

    async def remove(self, channel_id: str, key_id: str):
        """移除指定渠道下的 OAuth 账号。"""
        # 修改原因：删除 OAuth 账号时同样需要限定渠道，避免删除其他渠道的同邮箱凭据。
        # 修改方式：只从 _state[channel_id] 删除 key_id，并清理对应 channel:key 刷新锁。
        # 目的：让前端删除 Key 与运行时凭据删除保持同一作用域。
        accounts = self._get_channel_accounts(channel_id)
        accounts.pop(key_id, None)
        self._locks.pop(self._lock_key(channel_id, key_id), None)
        await self._persist()

    async def _persist(self):
        """原子写 oauth_state.json，避免阻塞事件循环。"""
        # 修改原因：refresh token rotation 后直接 open("w") 写正式文件，进程崩溃可能截断 oauth_state.json。
        # 修改方式：把 JSON 序列化、临时文件写入、fsync 和 os.replace 放在线程中执行，事件循环只等待结果。
        # 目的：确保刷新后的凭据要么完整替换旧文件，要么保留旧文件，不产生半写入状态。
        await asyncio.to_thread(self._write_state_atomic)

    def _write_state_atomic(self) -> None:
        """同步执行 oauth_state.json 原子写。"""
        # 修改原因：os.replace 只有在同目录临时文件上才能可靠保持原子替换语义。
        # 修改方式：在状态文件同目录创建 .tmp 文件，写入并 fsync 后再替换正式路径。
        # 目的：降低机器断电、进程崩溃或写盘异常导致 OAuth 凭据永久损坏的风险。
        data = json.dumps(self._state, indent=2, ensure_ascii=False)
        state_path = self._state_path
        dir_path = os.path.dirname(state_path) or "."
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, state_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        try:
            os.chmod(state_path, 0o600)
        except OSError:
            pass

    def _lock_key(self, channel_id: str | None, key_id: str) -> str:
        """生成渠道级刷新锁 key。"""
        # 修改原因：同邮箱可能存在于多个渠道，锁如果只按 key_id 会把不同渠道的刷新互相阻塞。
        # 修改方式：锁 key 固定为 f"{channel_id}:{key_id}"，channel_id 先按状态访问规则规范化。
        # 目的：保证同渠道同账号串行刷新，不同渠道账号互不影响。
        return f"{self._normalize_channel_id(channel_id)}:{key_id}"

    def _get_lock(self, channel_id: str, key_id: str):
        """按渠道和账号创建刷新锁，防止并发刷新同一个 refresh_token。"""
        lock_key = self._lock_key(channel_id, key_id)
        if lock_key not in self._locks:
            self._locks[lock_key] = asyncio.Lock()
        return self._locks[lock_key]
