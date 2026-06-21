"""Scope × Metric 正交化配额运行时。

修改原因：旧运行时为 request、cost、token、token_in、token_out、ip_rate 分别维护多套字典，新增任意作用域和度量组合都会继续复制代码。
修改方式：新增 ScopedStore，所有普通计数都用 (scope_id, metric_name) 作为统一 key；QuotaCounter 只负责把 Rule 映射到 scope_id 和读写类型。
目的：让 key、per-IP、per-model 的 request/cost/token 配额复用同一套检查、记录和状态输出逻辑，同时保留 unique_ip 的特殊去重语义。
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict, deque
from time import time
from typing import Dict, List, Optional, Set, Tuple, Union

from .parser import Limit, Metric, Rule, Scope, WindowType

logger = logging.getLogger('Zoaholic')
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SNAPSHOT_PATH = os.path.join(_PROJECT_ROOT, 'data', 'runtime_quota_snapshot.json')


# ──────────────────────────────────────────────
# 内部计数器
# ──────────────────────────────────────────────

class _SlidingCounter:
    """滑动窗口：存时间戳，count = 窗口内条目数。"""

    __slots__ = ('_q',)

    def __init__(self):
        self._q: deque = deque(maxlen=50000)

    def add(self, now: float):
        self._q.append(now)

    def count(self, now: float, period: float) -> int:
        if math.isinf(period):
            return len(self._q)
        cutoff = now - period
        return sum(1 for t in self._q if t > cutoff)

    def clear(self):
        self._q.clear()

    def cleanup(self, now: float, max_period: float):
        if math.isinf(max_period):
            return
        cutoff = now - max_period
        while self._q and self._q[0] <= cutoff:
            self._q.popleft()


class _SlidingAccumulator:
    """滑动窗口：存 (timestamp, amount)，total = 窗口内累加值。"""

    __slots__ = ('_q', '_perm')

    def __init__(self):
        self._q: deque = deque(maxlen=200000)
        self._perm: float = 0.0

    def add(self, now: float, amount: float):
        self._q.append((now, amount))
        self._perm += amount

    def total(self, now: float, period: float) -> float:
        if math.isinf(period):
            return self._perm
        cutoff = now - period
        return sum(a for t, a in self._q if t > cutoff)

    def clear(self):
        self._q.clear()
        self._perm = 0.0

    def cleanup(self, now: float, max_period: float):
        if math.isinf(max_period):
            return
        cutoff = now - max_period
        while self._q and self._q[0][0] <= cutoff:
            self._q.popleft()

    def set_permanent(self, total: float):
        """从 DB 恢复永久累计值（重启后调用）。"""
        self._perm = total


class _FixedWindow:
    """固定窗口：到期重置。"""

    __slots__ = ('val', 'start')

    def __init__(self):
        self.val: float = 0
        self.start: float = 0

    def get(self, now: float, period: float) -> float:
        if math.isinf(period):
            return self.val
        if self.start == 0 or now >= self.start + period:
            return 0
        return self.val

    def increment(self, now: float, period: float, amount: float = 1):
        if not math.isinf(period) and (self.start == 0 or now >= self.start + period):
            self.val = 0
            self.start = now
        self.val += amount

    def clear(self):
        self.val = 0
        self.start = 0

    def resets_at(self, period: float) -> Optional[float]:
        if math.isinf(period) or self.start == 0:
            return None
        return self.start + period


class _FixedUniqueWindow:
    """固定窗口内的 IP 去重集合。"""

    __slots__ = ('seen', 'start')

    def __init__(self):
        self.seen: Set[str] = set()
        self.start: float = 0

    def _ensure_window(self, now: float, period: float):
        if not math.isinf(period) and (self.start == 0 or now >= self.start + period):
            self.seen.clear()
            self.start = now

    def count(self, now: float, period: float) -> int:
        self._ensure_window(now, period)
        return len(self.seen)

    def contains(self, now: float, period: float, ip: str) -> bool:
        self._ensure_window(now, period)
        return ip in self.seen

    def add(self, now: float, period: float, ip: str):
        self._ensure_window(now, period)
        self.seen.add(ip)

    def clear(self):
        self.seen.clear()
        self.start = 0

    def resets_at(self, period: float) -> Optional[float]:
        if math.isinf(period) or self.start == 0:
            return None
        return self.start + period


class ScopedStore:
    """统一计数器存储。key = (scope_id, metric_name)。"""

    def __init__(self):
        self._sliding: Dict[Tuple[str, str], Union[_SlidingCounter, _SlidingAccumulator]] = {}
        self._fixed: Dict[Tuple[str, str], Dict[float, _FixedWindow]] = {}

    def get_or_create_counter(self, scope_id: str, metric: str) -> _SlidingCounter:
        key = (scope_id, metric)
        existing = self._sliding.get(key)
        if existing is None:
            existing = _SlidingCounter()
            self._sliding[key] = existing
        if not isinstance(existing, _SlidingCounter):
            raise TypeError(f"ScopedStore key {key!r} is not a counter")
        return existing

    def get_or_create_accumulator(self, scope_id: str, metric: str) -> _SlidingAccumulator:
        key = (scope_id, metric)
        existing = self._sliding.get(key)
        if existing is None:
            existing = _SlidingAccumulator()
            self._sliding[key] = existing
        if not isinstance(existing, _SlidingAccumulator):
            raise TypeError(f"ScopedStore key {key!r} is not an accumulator")
        return existing

    def get_fixed_window(self, scope_id: str, metric: str, period: float) -> _FixedWindow:
        key = (scope_id, metric)
        if key not in self._fixed:
            self._fixed[key] = {}
        if period not in self._fixed[key]:
            self._fixed[key][period] = _FixedWindow()
        return self._fixed[key][period]

    def scope_ids_for_metric(self, metric: str) -> Set[str]:
        """返回某个 metric 下已经出现过的 scope_id。"""

        ids = {scope_id for (scope_id, metric_name) in self._sliding.keys() if metric_name == metric}
        ids.update(scope_id for (scope_id, metric_name) in self._fixed.keys() if metric_name == metric)
        return ids

    def reset_scope_metric(self, scope_predicate, metric_name: str) -> int:
        """重置指定 scope_id 与 metric 的所有滑动和固定计数器。"""

        affected = 0
        for key, counter in list(self._sliding.items()):
            scope_id, stored_metric = key
            if stored_metric == metric_name and scope_predicate(scope_id):
                counter.clear()
                affected += 1
        for key, windows in list(self._fixed.items()):
            scope_id, stored_metric = key
            if stored_metric == metric_name and scope_predicate(scope_id):
                for window in windows.values():
                    window.clear()
                affected += len(windows) or 1
        return affected

    def cleanup(self, now: float, max_periods: Dict[str, float]):
        """清理普通滑动窗口。"""

        # 修改原因：ScopedStore 不再知道 Rule 结构，只能按 metric_name 接收外层计算好的最大保留窗口。
        # 修改方式：QuotaCounter 汇总每个 metric 的最大有限 period 后传入，store 只负责按类型调用 cleanup。
        # 目的：保留旧运行时的周期清理能力，同时避免 store 反向依赖 parser 规则。
        for (scope_id, metric_name), counter in list(self._sliding.items()):
            max_period = max_periods.get(metric_name, 3600)
            counter.cleanup(now, max_period)


# ──────────────────────────────────────────────
# QuotaCounter
# ──────────────────────────────────────────────

_ACCUMULATOR_METRICS = {Metric.COST, Metric.TOKEN, Metric.TOKEN_IN, Metric.TOKEN_OUT}
_TOKEN_METRICS = {Metric.TOKEN, Metric.TOKEN_IN, Metric.TOKEN_OUT}
_ALL_METRICS = [Metric.REQUEST, Metric.COST, Metric.TOKEN, Metric.TOKEN_IN, Metric.TOKEN_OUT, Metric.UNIQUE_IP]
_LABEL_QUALIFIERS = {'default', 'daily', 'day', 'hourly', 'hour', 'weekly', 'week', 'monthly', 'month', 'yearly', 'year'}


class QuotaCounter:
    """单个 API Key 的配额执行器。"""

    def __init__(self, rules: List[Rule]):
        self.rules = rules
        self._by_metric: Dict[Metric, List[Rule]] = defaultdict(list)
        for rule in rules:
            self._by_metric[rule.metric].append(rule)

        self._store = ScopedStore()
        # 修改原因：unique_ip 的计数对象不是普通数值，而是窗口内去重 IP 集合。
        # 修改方式：滑动窗口用 ip -> last_seen，固定窗口按 period 维护独立 set。
        # 目的：保持 IP 数量上限的特殊语义，同时不污染普通 ScopedStore 计数结构。
        self._ip_seen: Dict[str, float] = {}
        self._ip_fixed: Dict[float, _FixedUniqueWindow] = {}
        self._ops = 0
        self._snapshot_dirty = False

    # ── 公开 API ──

    def check_request(self, model: str = 'default', client_ip: str = '') -> Optional[str]:
        """请求前检查 + 记录。None = 放行，str = 拒绝原因。"""

        now = time()
        self._ops += 1
        if self._ops % 200 == 0:
            self._cleanup(now)

        normalized_model = model or 'default'
        normalized_ip = client_ip or ''

        # 修改原因：新模型要求 request、cost、token、unique_ip 都按 Rule 的 scope × metric 统一检查。
        # 修改方式：遍历所有规则，由 _scope_id_for_rule 判断本次请求是否命中该规则，再按 metric 类型读取当前值。
        # 目的：让 key 级、per-IP 和 model 级限制都能在请求进入上游前拦截。
        for metric in _ALL_METRICS:
            for rule in self._by_metric.get(metric, []):
                reason = self._check_rule(rule, normalized_model, normalized_ip, now)
                if reason:
                    return reason

        self._record_metric_event(Metric.REQUEST, 1, normalized_model, normalized_ip, now)
        self._record_unique_ip(normalized_ip, now)
        return None

    def is_exhausted(self, model: str = 'default') -> bool:
        """只检查 key 级 cost/token 是否耗尽（不记录、不检查 request 限速）。"""

        now = time()
        normalized_model = model or 'default'
        for metric in (Metric.COST, Metric.TOKEN, Metric.TOKEN_IN, Metric.TOKEN_OUT):
            for rule in self._by_metric.get(metric, []):
                if rule.scope != Scope.KEY:
                    continue
                reason = self._check_rule(rule, normalized_model, '', now)
                if reason:
                    return True
        return False

    def record_usage(self, model: str, cost: float = 0.0, tokens: int = 0,
                     tokens_in: int = 0, tokens_out: int = 0, client_ip: str = ''):
        """请求完成后记录 cost / token 用量。"""

        now = time()
        normalized_model = model or 'default'
        normalized_ip = client_ip or ''
        # 修改原因：per-IP cost/token 需要在请求完成后按客户端 IP 累加，旧接口没有 client_ip 无法定位 scope_id。
        # 修改方式：record_usage 接收 client_ip，并对每个 metric 调用同一套 _record_metric_event。
        # 目的：让 key scope 和 ip scope 的 cost/token/token_in/token_out 使用同一条记录路径。
        if cost > 0:
            self._record_metric_event(Metric.COST, float(cost), normalized_model, normalized_ip, now)
        if tokens > 0:
            self._record_metric_event(Metric.TOKEN, float(tokens), normalized_model, normalized_ip, now)
        if tokens_in > 0:
            self._record_metric_event(Metric.TOKEN_IN, float(tokens_in), normalized_model, normalized_ip, now)
        if tokens_out > 0:
            self._record_metric_event(Metric.TOKEN_OUT, float(tokens_out), normalized_model, normalized_ip, now)

    def restore_cost(self, qualifier: str, total: float):
        """从 DB 恢复 key 级 cost 永久累计值（重启后调用）。"""

        # 修改原因：新 ScopedStore 中 key 级 cost 不再按 qualifier 拆分存储，qualifier 只用于状态输出和多规则标签。
        # 修改方式：把恢复值写入 scope_id='key'、metric='cost' 的永久 accumulator。
        # 目的：保持旧 restore_cost 调用兼容，同时让所有 key 级 cost 规则共享同一累计费用。
        self._store.get_or_create_accumulator('key', Metric.COST.value).set_permanent(total)

    def get_status(self, model: str = 'default') -> dict:
        """获取当前配额状态（供 API 返回）。"""

        now = time()
        normalized_model = model or 'default'
        out: dict = {}
        for rule in self.rules:
            if rule.scope == Scope.MODEL and normalized_model != 'default' and not _model_match(rule.qualifier, normalized_model):
                continue
            for limit in rule.limits:
                status_key = f"{rule.scope.value}:{rule.metric.value}:{rule.qualifier}"
                if rule.metric == Metric.UNIQUE_IP:
                    current = self._unique_ip_count(limit, now)
                    reset_at = self._unique_ip_reset_at(limit, now)
                    out[status_key] = _status(current, limit, reset_at=reset_at, now=now)
                    continue
                if rule.scope == Scope.IP:
                    out[status_key] = self._aggregate_ip_status(rule.metric, limit, now)
                    continue
                if rule.scope == Scope.MODEL:
                    out[status_key] = self._aggregate_model_status(rule.qualifier, limit, now)
                    continue
                scope_id = self._scope_id_for_rule(rule, normalized_model, '', for_status=True)
                out[status_key] = self._status_for_scope(scope_id or 'key', rule.metric, limit, now)
        return out

    def reset_status(self, status_key: str) -> dict:
        """按状态 key 手动清零一条正交配额。"""

        # 修改原因：管理端需要对每个 Scope × Metric 状态提供手动重置按钮。
        # 修改方式：沿用 get_status 输出的 status_key，定位到底层 scope_id 与 metric 计数器后清零。
        # 目的：避免通过保存配置或重启服务来间接重置额度，同时保持 Key、Per-IP、模型三个作用域统一。
        scope, metric, qualifier = _parse_status_key(status_key)
        affected = 0
        if metric == Metric.UNIQUE_IP:
            affected = self._reset_unique_ip()
        elif scope == Scope.KEY:
            affected = self._store.reset_scope_metric(lambda scope_id: scope_id == 'key', metric.value)
        elif scope == Scope.IP:
            affected = self._store.reset_scope_metric(lambda scope_id: scope_id.startswith('ip:') and scope_id != 'ip:', metric.value)
        elif scope == Scope.MODEL:
            affected = self._reset_model_metric(qualifier, metric)
        else:
            raise ValueError(f"Unsupported quota scope: {scope}")

        self._snapshot_dirty = True
        return {
            'status_key': status_key,
            'scope': scope.value,
            'metric': metric.value,
            'qualifier': qualifier,
            'affected': affected,
        }

    # ── 检查 ──

    def _check_rule(self, rule: Rule, model: str, client_ip: str, now: float) -> Optional[str]:
        if rule.metric == Metric.UNIQUE_IP:
            return self._check_unique_ip(rule, client_ip, now)
        scope_id = self._scope_id_for_rule(rule, model, client_ip)
        if not scope_id:
            return None
        for limit in rule.limits:
            current = self._read_metric(scope_id, rule.metric, limit, now)
            if current >= limit.value:
                return _limit_message(rule, current, limit)
        return None

    def _check_unique_ip(self, rule: Rule, client_ip: str, now: float) -> Optional[str]:
        if not client_ip:
            return None
        for limit in rule.limits:
            current = self._unique_ip_count(limit, now)
            already_seen = self._unique_ip_contains(limit, client_ip, now)
            if not already_seen and current >= limit.value:
                return f"Too many IPs (max {int(limit.value)})"
        return None

    # ── 记录 ──

    def _record_metric_event(self, metric: Metric, amount: float, model: str, client_ip: str, now: float):
        sliding_done: Set[Tuple[str, str]] = set()
        fixed_done: Set[Tuple[str, str, float]] = set()
        for rule in self._by_metric.get(metric, []):
            scope_id = self._scope_id_for_rule(rule, model, client_ip)
            if not scope_id:
                continue
            for limit in rule.limits:
                metric_name = metric.value
                if limit.window == WindowType.SLIDING:
                    key = (scope_id, metric_name)
                    if key in sliding_done:
                        continue
                    if metric == Metric.REQUEST:
                        self._store.get_or_create_counter(scope_id, metric_name).add(now)
                    else:
                        self._store.get_or_create_accumulator(scope_id, metric_name).add(now, amount)
                    sliding_done.add(key)
                else:
                    key = (scope_id, metric_name, limit.period)
                    if key in fixed_done:
                        continue
                    increment = 1 if metric == Metric.REQUEST else amount
                    window = self._store.get_fixed_window(scope_id, metric_name, limit.period)
                    old_start = window.start
                    window.increment(now, limit.period, increment)
                    if window.start != old_start:
                        self._snapshot_dirty = True
                    fixed_done.add(key)

    def _record_unique_ip(self, client_ip: str, now: float):
        if not client_ip or not self._by_metric.get(Metric.UNIQUE_IP):
            return
        self._ip_seen[client_ip] = now
        for rule in self._by_metric.get(Metric.UNIQUE_IP, []):
            for limit in rule.limits:
                if limit.window == WindowType.FIXED:
                    window = self._ip_fixed_window(limit.period)
                    old_start = window.start
                    window.add(now, limit.period, client_ip)
                    if window.start != old_start:
                        self._snapshot_dirty = True

    # ── 读取 ──

    def _scope_id_for_rule(self, rule: Rule, model: str, client_ip: str, for_status: bool = False) -> Optional[str]:
        if rule.scope == Scope.KEY:
            if rule.metric in _TOKEN_METRICS and not _key_metric_qualifier_matches(rule.qualifier, model):
                return None
            return 'key'
        if rule.scope == Scope.IP:
            return f"ip:{client_ip}" if client_ip else (None if not for_status else 'ip:')
        if rule.scope == Scope.MODEL:
            if _model_match(rule.qualifier, model):
                return f"model:{model}"
            return None
        return None

    def _read_metric(self, scope_id: str, metric: Metric, limit: Limit, now: float) -> float:
        metric_name = metric.value
        if limit.window == WindowType.FIXED:
            return self._store.get_fixed_window(scope_id, metric_name, limit.period).get(now, limit.period)
        if metric == Metric.REQUEST:
            return self._store.get_or_create_counter(scope_id, metric_name).count(now, limit.period)
        if metric in _ACCUMULATOR_METRICS:
            return self._store.get_or_create_accumulator(scope_id, metric_name).total(now, limit.period)
        return 0

    def _status_for_scope(self, scope_id: str, metric: Metric, limit: Limit, now: float) -> dict:
        metric_name = metric.value
        current = self._read_metric(scope_id, metric, limit, now)
        reset_at = None
        if limit.window == WindowType.FIXED:
            reset_at = self._store.get_fixed_window(scope_id, metric_name, limit.period).resets_at(limit.period)
        return _status(current, limit, reset_at=reset_at, now=now)

    def _aggregate_ip_status(self, metric: Metric, limit: Limit, now: float) -> dict:
        # 修改原因：ip scope 的真实计数分散在 ip:{client_ip} 多个 scope_id 下，API 不能为每个 IP 生成动态顶层 key。
        # 修改方式：状态聚合为同一 key（如 ip:cost:default），current 使用最接近触发限制的单个 IP 最大值，并附加 subjects 数量。
        # 目的：前端可以稳定展示 per-IP 规则，同时仍能看到当前最紧张的 IP 使用量。
        metric_name = metric.value
        scope_ids = sorted(sid for sid in self._store.scope_ids_for_metric(metric_name) if sid.startswith('ip:') and sid != 'ip:')
        best_current = 0
        best_reset_at = None
        for scope_id in scope_ids:
            current = self._read_metric(scope_id, metric, limit, now)
            if current >= best_current:
                best_current = current
                best_reset_at = None
                if limit.window == WindowType.FIXED:
                    best_reset_at = self._store.get_fixed_window(scope_id, metric_name, limit.period).resets_at(limit.period)
        data = _status(best_current, limit, reset_at=best_reset_at, now=now)
        data['aggregate'] = 'max'
        data['subjects'] = len(scope_ids)
        return data

    def _aggregate_model_status(self, qualifier: str, limit: Limit, now: float) -> dict:
        metric_name = Metric.REQUEST.value
        scope_ids = sorted(
            sid for sid in self._store.scope_ids_for_metric(metric_name)
            if sid.startswith('model:') and _model_match(qualifier, sid.split(':', 1)[1])
        )
        active_scope_ids = set(self._store.scope_ids_for_metric(metric_name))
        if not scope_ids:
            scope_ids = [f"model:{qualifier}"]
        best_current = 0
        best_reset_at = None
        for scope_id in scope_ids:
            current = self._read_metric(scope_id, Metric.REQUEST, limit, now)
            if current >= best_current:
                best_current = current
                best_reset_at = None
                if limit.window == WindowType.FIXED:
                    best_reset_at = self._store.get_fixed_window(scope_id, metric_name, limit.period).resets_at(limit.period)
        data = _status(best_current, limit, reset_at=best_reset_at, now=now)
        data['aggregate'] = 'max'
        data['subjects'] = len([sid for sid in scope_ids if sid in active_scope_ids])
        return data

    def _unique_ip_count(self, limit: Limit, now: float) -> int:
        if limit.window == WindowType.FIXED:
            return self._ip_fixed_window(limit.period).count(now, limit.period)
        if math.isinf(limit.period):
            return len(self._ip_seen)
        cutoff = now - limit.period
        return sum(1 for last_seen in self._ip_seen.values() if last_seen > cutoff)

    def _unique_ip_contains(self, limit: Limit, client_ip: str, now: float) -> bool:
        if limit.window == WindowType.FIXED:
            return self._ip_fixed_window(limit.period).contains(now, limit.period, client_ip)
        if math.isinf(limit.period):
            return client_ip in self._ip_seen
        return self._ip_seen.get(client_ip, 0) > now - limit.period

    def _unique_ip_reset_at(self, limit: Limit, now: float) -> Optional[float]:
        if limit.window != WindowType.FIXED:
            return None
        return self._ip_fixed_window(limit.period).resets_at(limit.period)

    def _reset_unique_ip(self) -> int:
        affected = len(self._ip_seen) + len(self._ip_fixed)
        self._ip_seen.clear()
        for window in self._ip_fixed.values():
            window.clear()
        return affected

    def _reset_model_metric(self, qualifier: str, metric: Metric) -> int:
        metric_name = metric.value
        known_scope_ids = self._store.scope_ids_for_metric(metric_name)
        matched = {
            scope_id for scope_id in known_scope_ids
            if scope_id.startswith('model:') and _model_match(qualifier, scope_id.split(':', 1)[1])
        }
        matched.add(f"model:{qualifier}")
        return self._store.reset_scope_metric(lambda scope_id: scope_id in matched, metric_name)

    def _ip_fixed_window(self, period: float) -> _FixedUniqueWindow:
        if period not in self._ip_fixed:
            self._ip_fixed[period] = _FixedUniqueWindow()
        return self._ip_fixed[period]

    # ── 快照 ──

    def _snapshot(self) -> dict:
        """导出固定窗口状态。"""
        snap: dict = {'fixed': [], 'ip_fixed': []}
        for (scope_id, metric_name), windows in self._store._fixed.items():
            for period, window in windows.items():
                if window.start > 0:
                    snap['fixed'].append({
                        's': scope_id, 'm': metric_name, 'p': period,
                        'v': window.val, 't': window.start,
                    })
        for period, uw in self._ip_fixed.items():
            if uw.start > 0:
                snap['ip_fixed'].append({
                    'p': period, 'ips': sorted(uw.seen), 't': uw.start,
                })
        return snap

    def _restore_snapshot(self, snap: dict):
        """从快照恢复固定窗口状态（跳过已过期窗口）。"""
        now = time()
        restored = 0
        for entry in snap.get('fixed', []):
            scope_id = entry.get('s', '')
            metric = entry.get('m', '')
            period = entry.get('p', 0)
            val = entry.get('v', 0)
            start = entry.get('t', 0)
            if not scope_id or not metric or period <= 0 or start <= 0:
                continue
            if not math.isinf(period) and now >= start + period:
                continue
            window = self._store.get_fixed_window(scope_id, metric, period)
            window.val = val
            window.start = start
            restored += 1
        for entry in snap.get('ip_fixed', []):
            period = entry.get('p', 0)
            ips = entry.get('ips', [])
            start = entry.get('t', 0)
            if period <= 0 or start <= 0:
                continue
            if not math.isinf(period) and now >= start + period:
                continue
            uw = self._ip_fixed_window(period)
            uw.seen = set(ips)
            uw.start = start
            restored += 1
        return restored

    # ── 清理 ──

    def _cleanup(self, now: float):
        max_periods: Dict[str, float] = {}
        for metric in _ALL_METRICS:
            periods = [limit.period for rule in self._by_metric.get(metric, []) for limit in rule.limits if limit.window == WindowType.SLIDING]
            if not periods:
                continue
            finite = [period for period in periods if not math.isinf(period)]
            max_periods[metric.value] = max(finite) if finite else math.inf
        self._store.cleanup(now, max_periods)

        unique_periods = [limit.period for rule in self._by_metric.get(Metric.UNIQUE_IP, []) for limit in rule.limits if limit.window == WindowType.SLIDING]
        finite_unique = [period for period in unique_periods if not math.isinf(period)]
        if finite_unique:
            cutoff = now - max(finite_unique)
            self._ip_seen = {ip: seen_at for ip, seen_at in self._ip_seen.items() if seen_at > cutoff}


# ──────────────────────────────────────────────
# QuotaRegistry
# ──────────────────────────────────────────────

class QuotaRegistry:
    """全局配额注册表，管理所有 API Key 的配额计数器。"""

    def __init__(self):
        self._counters: Dict[str, QuotaCounter] = {}
        self._global: Optional[QuotaCounter] = None

    def init_from_config(self, config: dict):
        """从应用配置初始化（启动或热更新时调用）。"""

        from .compat import merge_legacy_to_quota
        from .parser import parse_config as _parse

        pre_snapshot = self._capture_snapshot()

        self._counters.clear()
        self._global = None

        gp = config.get('preferences') or {}
        gq = merge_legacy_to_quota(gp)
        if gq:
            rules = _parse(gq)
            if rules:
                self._global = QuotaCounter(rules)

        for kc in config.get('api_keys', []):
            api = kc.get('api', '')
            if not api:
                continue
            prefs = kc.get('preferences') or {}
            q = merge_legacy_to_quota(prefs)
            if q:
                rules = _parse(q)
                if rules:
                    self._counters[api] = QuotaCounter(rules)

        snapshot = pre_snapshot if pre_snapshot.get('c') or pre_snapshot.get('g') else self._read_snapshot_file()
        self._apply_snapshot(snapshot)
        self._save_snapshot()

    def check(self, api_key: str, model: str = 'default', client_ip: str = '') -> Optional[str]:
        """请求前检查。None = 放行，str = 拒绝原因（含计数记录）。"""

        if self._global:
            reason = self._global.check_request(model, client_ip)
            if reason:
                self._check_and_save()
                return f"[global] {reason}"
        counter = self._counters.get(api_key)
        if counter:
            reason = counter.check_request(model, client_ip)
            if reason:
                self._check_and_save()
                return reason
        self._check_and_save()
        return None

    def is_exhausted(self, api_key: str, model: str = 'default') -> bool:
        """检查 key 级 cost/token 是否耗尽（不记录请求计数）。用于 middleware 预检。"""

        counter = self._counters.get(api_key)
        return counter.is_exhausted(model) if counter else False

    def record_usage(self, api_key: str, model: str, cost: float = 0.0, tokens: int = 0,
                     tokens_in: int = 0, tokens_out: int = 0, client_ip: str = ''):
        """请求完成后记录用量。"""

        # 修改原因：per-IP cost/token 规则需要知道 client_ip；旧接口只传 cost/tokens 会导致 ip scope 永远无法累加。
        # 修改方式：接口追加 tokens_in、tokens_out 和 client_ip，并向 key 计数器与 global 计数器同步传递。
        # 目的：让统计写入后的真实用量可以覆盖 key scope 与 ip scope 的全部 token/cost metric。
        counter = self._counters.get(api_key)
        if counter:
            counter.record_usage(model, cost, tokens, tokens_in=tokens_in, tokens_out=tokens_out, client_ip=client_ip)
        if self._global:
            self._global.record_usage(model, cost, tokens, tokens_in=tokens_in, tokens_out=tokens_out, client_ip=client_ip)
        self._check_and_save()

    def get_counter(self, api_key: str) -> Optional[QuotaCounter]:
        return self._counters.get(api_key)

    def get_key_status(self, api_key: str, model: str = 'default') -> dict:
        counter = self._counters.get(api_key)
        return counter.get_status(model) if counter else {}

    def reset_key_status(self, api_key: str, status_key: str) -> dict:
        counter = self._counters.get(api_key)
        if not counter:
            raise KeyError(api_key)
        result = counter.reset_status(status_key)
        result['api_key'] = api_key
        self._save_snapshot()
        return result

    def has_quota(self, api_key: str) -> bool:
        return api_key in self._counters

    # ── 快照持久化 ──

    def _capture_snapshot(self) -> dict:
        data: dict = {'v': 1, 't': time(), 'c': {}}
        for api_key, counter in self._counters.items():
            snap = counter._snapshot()
            if snap.get('fixed') or snap.get('ip_fixed'):
                data['c'][api_key] = snap
        if self._global:
            snap = self._global._snapshot()
            if snap.get('fixed') or snap.get('ip_fixed'):
                data['g'] = snap
        return data

    def _apply_snapshot(self, data: dict):
        if not data or data.get('v') != 1:
            return
        total = 0
        for api_key, snap in data.get('c', {}).items():
            counter = self._counters.get(api_key)
            if counter:
                total += counter._restore_snapshot(snap)
        if self._global and data.get('g'):
            total += self._global._restore_snapshot(data['g'])
        if total > 0:
            logger.info(f"Restored {total} fixed quota windows from snapshot")

    def _save_snapshot(self):
        data = self._capture_snapshot()
        try:
            os.makedirs(os.path.dirname(_SNAPSHOT_PATH), exist_ok=True)
            tmp = _SNAPSHOT_PATH + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))
            os.replace(tmp, _SNAPSHOT_PATH)
        except Exception as e:
            logger.warning(f"Failed to save quota snapshot: {e}")

    def _read_snapshot_file(self) -> dict:
        try:
            with open(_SNAPSHOT_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _check_and_save(self):
        dirty = False
        for counter in self._counters.values():
            if counter._snapshot_dirty:
                counter._snapshot_dirty = False
                dirty = True
        if self._global and self._global._snapshot_dirty:
            self._global._snapshot_dirty = False
            dirty = True
        if dirty:
            self._save_snapshot()


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def _parse_status_key(status_key: str) -> Tuple[Scope, Metric, str]:
    raw = str(status_key or '').strip()
    parts = raw.split(':', 2)
    if len(parts) != 3:
        raise ValueError(f"Invalid quota status key: {status_key!r}")
    scope_raw, metric_raw, qualifier = parts
    try:
        scope = Scope(scope_raw)
        metric = Metric(metric_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid quota status key: {status_key!r}") from exc
    if not qualifier:
        qualifier = 'default'
    return scope, metric, qualifier


def _model_match(qualifier: str, model: str) -> bool:
    """模型匹配：精确 → 包含 → default 通配。"""

    if qualifier == 'default':
        return True
    if qualifier == model:
        return True
    return qualifier in model


def _key_metric_qualifier_matches(qualifier: str, model: str) -> bool:
    """判断 key scope token 类 qualifier 是否匹配本次模型。"""

    # 修改原因：旧 token:claude 代表模型限定 token 规则，而新 cost:daily 代表标签规则。
    # 修改方式：常见时间标签和 default 视为全模型规则，其它 token qualifier 继续按模型名匹配。
    # 目的：兼容旧 token 模型规则，同时支持新正交状态 key 的 qualifier 标签。
    if qualifier in _LABEL_QUALIFIERS:
        return True
    return _model_match(qualifier, model)


def _period_str(period: float) -> str:
    if math.isinf(period):
        return 'inf'
    if period >= 86400:
        days = period / 86400
        return f"{int(days)}d" if days == int(days) else f"{days}d"
    if period >= 3600:
        hours = period / 3600
        return f"{int(hours)}h" if hours == int(hours) else f"{hours}h"
    if period >= 60:
        minutes = period / 60
        return f"{int(minutes)}min" if minutes == int(minutes) else f"{minutes}min"
    return f"{int(period)}s"


def _status(current: float, limit: Limit, reset_at: Optional[float] = None, now: Optional[float] = None) -> dict:
    display_current = current if isinstance(current, int) else round(current, 4)
    remaining = max(0, limit.value - current)
    data = {
        'current': display_current,
        'limit': limit.value,
        'period': limit.period if not math.isinf(limit.period) else 'inf',
        'window': limit.window.value,
        'remaining': remaining if isinstance(remaining, int) else round(remaining, 4),
        'label': f"{_format_limit_value(limit.value)}/{_period_str(limit.period)}",
    }
    if reset_at is not None:
        base_now = time() if now is None else now
        if reset_at > base_now:
            data['reset_at'] = round(reset_at, 3)
            data['reset_in'] = round(reset_at - base_now, 3)
    return data


def _format_limit_value(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return str(value)


def _limit_message(rule: Rule, current: float, limit: Limit) -> str:
    if rule.metric == Metric.REQUEST:
        if rule.scope == Scope.IP:
            return f"IP rate limited ({int(limit.value)}/{_period_str(limit.period)})"
        return f"Request rate limited ({int(limit.value)}/{_period_str(limit.period)})"
    if rule.metric == Metric.COST:
        return f"Cost quota exceeded (${current:.2f}/${limit.value:.2f})"
    if rule.metric == Metric.TOKEN:
        return f"Token quota exceeded ({int(current)}/{int(limit.value)})"
    if rule.metric == Metric.TOKEN_IN:
        return f"Token input quota exceeded ({int(current)}/{int(limit.value)})"
    if rule.metric == Metric.TOKEN_OUT:
        return f"Token output quota exceeded ({int(current)}/{int(limit.value)})"
    return f"Quota exceeded ({int(current)}/{int(limit.value)})"
