"""
Channel cooldown manager.

负责记录 provider/model 冷却状态，并根据冷却时间过滤不可用的 provider。
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import List, Dict, Any


class ChannelManager:
    """
    管理各个 provider/model 的冷却状态：
    - exclude_model: 将指定 provider/model 标记为在一段时间内不可用
    - is_model_excluded: 判断某个 provider/model 当前是否仍在冷却期内
    - get_available_providers: 从 provider 列表中过滤掉冷却中的模型
    """

    def __init__(self, cooldown_period: int = 300) -> None:
        # key: "provider/model" -> value: datetime 上次被标记不可用的时间
        self._excluded_models = defaultdict(lambda: None)
        self.cooldown_period = cooldown_period

    async def exclude_model(self, provider: str, model: str) -> None:
        """
        将指定 provider/model 标记为不可用，记录当前时间。
        """
        model_key = f"{provider}/{model}"
        self._excluded_models[model_key] = datetime.now()

    async def is_model_excluded(self, provider: str, model: str, cooldown_period: int = 0) -> bool:
        """
        判断指定 provider/model 是否仍在冷却期内。

        Args:
            provider: 渠道名称
            model: 模型名称（target model）
            cooldown_period: 冷却时间（秒），如果为 0 则使用实例默认值
        """
        model_key = f"{provider}/{model}"
        excluded_time = self._excluded_models[model_key]
        if not excluded_time:
            return False

        period = cooldown_period or self.cooldown_period
        if datetime.now() - excluded_time > timedelta(seconds=period):
            # 冷却时间已过，清理记录
            del self._excluded_models[model_key]
            return False
        return True

    async def get_available_providers(self, providers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        过滤出可用的 providers，仅排除处于冷却期的模型。

        providers 的结构示例：
        {
            "provider": "openai",
            "model": [{"gpt-4": "gpt-4"}],
            "preferences": {"cooldown_period": 300}
        }
        """
        available_providers: List[Dict[str, Any]] = []
        for provider in providers:
            provider_name = provider["provider"]
            # 获取唯一的模型映射字典
            model_dict = provider["model"][0]
            # target_model 为代理到真实 provider 的目标模型名
            target_model = list(model_dict.values())[0]
            period = provider.get("preferences", {}).get("cooldown_period", self.cooldown_period)

            # 检查该模型是否被排除
            if not await self.is_model_excluded(provider_name, target_model, period):
                available_providers.append(provider)

        return available_providers