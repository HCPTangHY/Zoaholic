"""OAuth provider 抽象基类。"""

from abc import ABC, abstractmethod
from typing import Literal


class OAuthProvider(ABC):
    """不同 OAuth 提供商的最小统一接口。"""

    # 修改原因：不同 OAuth provider 对 redirect_uri 的限制不同，authorize 路由不能再硬编码一种回调方式。
    # 修改方式：基类提供默认 auto 声明，子类用类属性覆盖为 manual 或其他固定模式。
    # 目的：让后端可以在前端不传 mode 时按 provider 自动选择直连回调或手动粘贴。
    redirect_mode: Literal["auto", "manual"] = "auto"

    # 修改原因：manual 模式需要使用 provider 白名单内的本机回调地址，而不是 Zoaholic 线上域名。
    # 修改方式：基类提供保守默认值，具体 provider 覆盖为自己的固定 localhost callback。
    # 目的：让手动粘贴模式能复用同一套 authorize 与 exchange 流程。
    localhost_redirect_uri: str = "http://localhost:8080/callback"

    @abstractmethod
    async def refresh_token(self, credential: dict, config: dict | None = None) -> dict:
        """刷新 access_token。"""
        # 修改原因：部分 provider 的 token endpoint 需要读取最新运行时配置，不能只依赖启动时对象状态。
        # 修改方式：基类签名增加可选 config 参数；不需要配置的 provider 可以忽略该参数。
        # 目的：让 OAuthManager 能在调用刷新时把 app.state.config 的当前值传给 provider。
        ...

    @abstractmethod
    def build_auth_url(self, state: str, redirect_uri: str) -> tuple[str, str]:
        """构建 OAuth 授权 URL。"""
        # 修改原因：authorize 路由必须同时拿到授权 URL 和 PKCE verifier，基类签名需要表达真实返回结构。
        # 修改方式：把返回类型标注更新为 tuple[str, str]，与 CodexProvider 的实现保持一致。
        # 目的：减少后续 provider 实现和调用方之间的接口歧义。
        ...

    @abstractmethod
    async def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        code_verifier: str | None = None,
        config: dict | None = None,
    ) -> dict:
        """授权码换 token。"""
        # 修改原因：授权码交换也会访问 token endpoint，部分 provider 需要当前运行时配置。
        # 修改方式：在保留可选 code_verifier 的基础上增加可选 config 参数。
        # 目的：让 OAuthManager 可以统一把最新配置传入 exchange_code，而不需要 provider 反向导入 main.app。
        ...

    async def fetch_quota(self, credential: dict, config: dict | None = None) -> dict | None:
        """获取账号额度信息；不支持时返回 None。"""
        # 修改原因：不同 OAuth provider 的额度来源不同，不能强制所有 provider 都实现主动查询。
        # 修改方式：基类提供可选默认实现，具体 provider 按需覆盖并通过 OAuthManager 注入当前配置。
        # 目的：让前端可以统一调用 quota API，同时保持不支持额度查询的 provider 兼容。
        return None

    @abstractmethod
    def get_default_base_url(self) -> str:
        """返回上游默认地址。"""
        # 修改原因：OAuth 渠道通常有内置上游地址。
        # 修改方式：由 provider 返回默认 base_url。
        # 目的：让注册和导入流程可以按 provider 获取默认目标。
        ...
