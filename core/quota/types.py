from dataclasses import dataclass, field
from typing import Any, Optional


# 修改原因：普通 API Key 余额与 OAuth quota 目前使用不同字段，前端难以复用同一套展示结构。
# 修改方式：新增 dataclass 数据模型，把进度条、标签和完整快照拆成统一的原生 Python 结构。
# 目的：在不引入 pydantic 和不破坏旧 BalanceResult 字段的前提下，为后续额度 UI 重构提供稳定契约。
@dataclass
class QuotaGauge:
    """一个额度弧线/进度条。"""

    id: str                          # 如 'balance', 'inner', 'outer', '5h', '7d'
    label: str                       # 如 '余额', '5h', '7d', 'Gemini', 'External'
    role: str = 'primary'            # primary / secondary / short_window / long_window
    percent: Optional[float] = None  # 0-100
    total: Optional[float] = None
    available: Optional[float] = None
    used: Optional[float] = None
    tone: Optional[str] = None       # green / yellow / red / 自动计算
    resets_at: Optional[str] = None  # ISO datetime
    unit: Optional[str] = None       # credits / tokens / requests
    display_mode: str = 'percent'    # percent / amount / quota


@dataclass
class QuotaBadge:
    """一个标签药丸。"""

    id: str               # 如 'oai_tier', 'plan_type', 'subscription'
    label: str            # 如 'Tier 3', 'Pro', 'Plus'
    tone: str = 'blue'    # blue / green / yellow / red / gray
    priority: int = 100   # 越大越优先显示
    source: str = ''      # 来源插件/渠道名


@dataclass
class QuotaSnapshot:
    """统一额度快照，普通 Key 和 OAuth Key 共用。"""

    supported: bool = True
    status: str = 'ok'        # ok / error / unknown / unsupported
    error: Optional[str] = None

    # 概要（兼容旧 BalanceResult 的核心字段）
    value_type: str = 'amount'   # amount / percent / quota
    total: Optional[float] = None
    used: Optional[float] = None
    available: Optional[float] = None
    percent: Optional[float] = None
    currency: Optional[str] = None
    expires_at: Optional[str] = None

    # 新结构
    gauges: list = field(default_factory=list)     # List[QuotaGauge]
    badges: list = field(default_factory=list)     # List[QuotaBadge]
    metrics: dict = field(default_factory=dict)    # 自由 KV，如 tpm/rpm
    extensions: dict = field(default_factory=dict) # 渠道/插件私有数据
    raw: Any = None

    # 兼容旧字段
    tier: Optional[str] = None
    quota_inner: Optional[float] = None
    quota_outer: Optional[float] = None

    def to_dict(self) -> dict:
        """输出为 API 响应字典，同时包含新旧字段。"""
        # 修改原因：API 需要同时服务旧前端字段和新 quota UI 字段。
        # 修改方式：先完整生成兼容字典，再剔除 None，保留空 gauges、badges、metrics 和 extensions 作为稳定新字段。
        # 目的：调用方可以直接把 QuotaSnapshot 输出合并进现有响应，而不会丢失旧字段。
        d = {
            'supported': self.supported,
            'status': self.status,
            'error': self.error,
            'value_type': self.value_type,
            'total': self.total,
            'used': self.used,
            'available': self.available,
            'percent': self.percent,
            'currency': self.currency,
            'expires_at': self.expires_at,
            'gauges': [_gauge_to_dict(g) for g in self.gauges],
            'badges': [_badge_to_dict(b) for b in self.badges],
            'metrics': self.metrics,
            'extensions': self.extensions,
            'raw': self.raw,
            # 兼容旧字段
            'tier': self.tier,
            'quota_inner': self.quota_inner,
            'quota_outer': self.quota_outer,
        }
        return {k: v for k, v in d.items() if v is not None}


def _gauge_to_dict(g) -> dict:
    # 修改原因：dataclass 实例不能直接作为 JSONResponse 内容的一部分稳定输出。
    # 修改方式：只导出前端需要的非空字段，并固定 id、label、role 三个基础字段。
    # 目的：保持 API 响应简洁，同时避免 Optional 字段以 null 形式干扰旧调用方。
    d = {'id': g.id, 'label': g.label, 'role': g.role}
    for attr in ('percent', 'total', 'available', 'used', 'tone', 'resets_at', 'unit', 'display_mode'):
        v = getattr(g, attr, None)
        if v is not None:
            d[attr] = v
    return d


def _badge_to_dict(b) -> dict:
    # 修改原因：badge 结构需要在响应中保持轻量、可排序，并保留来源信息。
    # 修改方式：统一输出 id、label、tone、priority 和 source 五个字段。
    # 目的：前端可以不理解具体插件，也能按优先级展示标签药丸。
    return {'id': b.id, 'label': b.label, 'tone': b.tone, 'priority': b.priority, 'source': b.source}
