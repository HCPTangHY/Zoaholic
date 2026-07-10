"""多维配额规则与运行时。

新规则把分组维度、过滤条件、度量、聚合方式和窗口分开表达。
旧 preferences.quota 继续由 legacy runtime 处理；本模块只处理
preferences.quota_rules，便于平滑迁移。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from fnmatch import fnmatchcase
from time import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .parser import Limit, WindowType, parse_value

logger = logging.getLogger("Zoaholic")


class Dimension(str, Enum):
    IP = "ip"
    MODEL = "model"


class Measure(str, Enum):
    REQUEST = "request"
    COST = "cost"
    TOKEN = "token"
    TOKEN_IN = "token_in"
    TOKEN_OUT = "token_out"
    IP = "ip"


class Aggregate(str, Enum):
    COUNT = "count"
    SUM = "sum"
    COUNT_DISTINCT = "count_distinct"


_DEFAULT_AGGREGATES = {
    Measure.REQUEST: Aggregate.COUNT,
    Measure.COST: Aggregate.SUM,
    Measure.TOKEN: Aggregate.SUM,
    Measure.TOKEN_IN: Aggregate.SUM,
    Measure.TOKEN_OUT: Aggregate.SUM,
    Measure.IP: Aggregate.COUNT_DISTINCT,
}


@dataclass
class DimensionalRule:
    id: str
    group_by: Tuple[Dimension, ...]
    where: Dict[str, str]
    measure: Measure
    aggregate: Aggregate
    limits: List[Limit] = field(default_factory=list)
    label: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "group_by": [dimension.value for dimension in self.group_by],
            "where": dict(self.where),
            "measure": self.measure.value,
            "aggregate": self.aggregate.value,
            "label": self.label,
        }


def _derived_rule_id(raw: dict, index: int) -> str:
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"qr_{index}_{digest}"


def parse_dimensional_rules(raw_rules: Any) -> List[DimensionalRule]:
    """解析 preferences.quota_rules；无效规则只记录警告。"""
    if not isinstance(raw_rules, list):
        return []

    parsed: List[DimensionalRule] = []
    seen_ids = set()
    if len(raw_rules) > 200:
        logger.warning("[quota] quota_rules contains %s entries; only the first 200 are loaded", len(raw_rules))
    for index, raw in enumerate(raw_rules[:200]):
        if not isinstance(raw, dict):
            logger.warning("[quota] Ignore non-object quota_rules[%s]", index)
            continue
        try:
            raw_group_by = raw.get("group_by") or []
            if not isinstance(raw_group_by, list):
                raise ValueError("group_by must be a list")
            group_by = tuple(dict.fromkeys(Dimension(str(value).strip().lower()) for value in raw_group_by))

            raw_where = raw.get("where") or {}
            if not isinstance(raw_where, dict):
                raise ValueError("where must be an object")
            where: Dict[str, str] = {}
            for key, value in raw_where.items():
                dimension = Dimension(str(key).strip().lower())
                if value is not None and str(value).strip():
                    where[dimension.value] = str(value).strip()

            measure = Measure(str(raw.get("measure") or "request").strip().lower())
            aggregate = Aggregate(str(raw.get("aggregate") or _DEFAULT_AGGREGATES[measure].value).strip().lower())
            if aggregate != _DEFAULT_AGGREGATES[measure]:
                raise ValueError(f"{measure.value} only supports {_DEFAULT_AGGREGATES[measure].value}")
            if aggregate == Aggregate.COUNT_DISTINCT and Dimension.IP in group_by:
                raise ValueError("count_distinct(ip) cannot also group by ip")

            limit_value = raw.get("limit")
            if not isinstance(limit_value, str) or not limit_value.strip():
                raise ValueError("limit is required")
            limits = parse_value(limit_value)

            rule_id = str(raw.get("id") or _derived_rule_id(raw, index)).strip()
            if not rule_id:
                raise ValueError("id is empty")
            if rule_id in seen_ids:
                raise ValueError(f"duplicate id {rule_id!r}")
            seen_ids.add(rule_id)

            parsed.append(DimensionalRule(
                id=rule_id,
                group_by=group_by,
                where=where,
                measure=measure,
                aggregate=aggregate,
                limits=limits,
                label=str(raw.get("label") or "").strip(),
            ))
        except (TypeError, ValueError) as exc:
            logger.warning("[quota] Invalid quota_rules[%s]: %s", index, exc)
    return parsed


class _FixedValue:
    __slots__ = ("value", "start")

    def __init__(self):
        self.value = 0.0
        self.start = 0.0

    def get(self, now: float, period: float) -> float:
        if not math.isinf(period) and (self.start == 0 or now >= self.start + period):
            return 0.0
        return self.value

    def add(self, now: float, period: float, amount: float):
        if not math.isinf(period) and (self.start == 0 or now >= self.start + period):
            self.value = 0.0
            self.start = now
        elif self.start == 0:
            self.start = now
        self.value += amount

    def reset_at(self, period: float) -> Optional[float]:
        if math.isinf(period) or self.start == 0:
            return None
        return self.start + period


class _FixedDistinct:
    __slots__ = ("values", "start")

    def __init__(self):
        self.values = set()
        self.start = 0.0

    def _ensure(self, now: float, period: float):
        if not math.isinf(period) and (self.start == 0 or now >= self.start + period):
            self.values.clear()
            self.start = now
        elif self.start == 0:
            self.start = now

    def count(self, now: float, period: float) -> int:
        self._ensure(now, period)
        return len(self.values)

    def contains(self, now: float, period: float, value: str) -> bool:
        self._ensure(now, period)
        return value in self.values

    def add(self, now: float, period: float, value: str):
        self._ensure(now, period)
        self.values.add(value)

    def reset_at(self, period: float) -> Optional[float]:
        if math.isinf(period) or self.start == 0:
            return None
        return self.start + period


@dataclass
class _Bucket:
    dimensions: Dict[str, str]
    events: deque = field(default_factory=lambda: deque(maxlen=200000))
    amounts: deque = field(default_factory=lambda: deque(maxlen=200000))
    permanent_amount: float = 0.0
    fixed: Dict[float, _FixedValue] = field(default_factory=dict)
    distinct_seen: Dict[str, float] = field(default_factory=dict)
    distinct_fixed: Dict[float, _FixedDistinct] = field(default_factory=dict)
    last_seen: float = 0.0


class DimensionalQuotaCounter:
    """一个 API Key 的 quota_rules 执行器。"""

    MAX_BUCKETS = 5000

    def __init__(self, rules: List[DimensionalRule]):
        self.rules = rules
        self._rules = {rule.id: rule for rule in rules}
        self._buckets: Dict[Tuple[str, str], _Bucket] = {}
        self._ops = 0
        self._snapshot_dirty = False

    @staticmethod
    def _context(model: str = "default", client_ip: str = "") -> Dict[str, str]:
        return {"model": model or "default", "ip": client_ip or ""}

    def _matches(self, rule: DimensionalRule, context: Dict[str, str]) -> bool:
        for key, pattern in rule.where.items():
            value = context.get(key, "")
            if key == Dimension.IP.value:
                if not _ip_matches(pattern, value):
                    return False
            elif not _text_matches(pattern, value):
                return False
        return True

    def _bucket_identity(self, rule: DimensionalRule, context: Dict[str, str]) -> Optional[Tuple[str, Dict[str, str]]]:
        dimensions: Dict[str, str] = {}
        for dimension in rule.group_by:
            value = context.get(dimension.value, "")
            if not value:
                return None
            dimensions[dimension.value] = value
        key = json.dumps(dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return key, dimensions

    def _get_bucket(self, rule: DimensionalRule, context: Dict[str, str], create: bool = True) -> Optional[_Bucket]:
        if not self._matches(rule, context):
            return None
        identity = self._bucket_identity(rule, context)
        if identity is None:
            return None
        bucket_key, dimensions = identity
        key = (rule.id, bucket_key)
        bucket = self._buckets.get(key)
        if bucket is None and create:
            if len(self._buckets) >= self.MAX_BUCKETS:
                raise OverflowError("Quota subject limit reached")
            bucket = _Bucket(dimensions=dimensions)
            self._buckets[key] = bucket
        return bucket

    def check_request(self, model: str = "default", client_ip: str = "") -> Optional[str]:
        now = time()
        context = self._context(model, client_ip)
        self._ops += 1
        if self._ops % 200 == 0:
            self._cleanup(now)

        matched: List[Tuple[DimensionalRule, _Bucket]] = []
        try:
            for rule in self.rules:
                bucket = self._get_bucket(rule, context)
                if bucket is None:
                    continue
                matched.append((rule, bucket))
                for limit in rule.limits:
                    current = self._read(rule, bucket, limit, now)
                    if rule.aggregate == Aggregate.COUNT_DISTINCT:
                        value = context.get(rule.measure.value, "")
                        if value and not self._distinct_contains(bucket, limit, value, now) and current >= limit.value:
                            return _limit_message(rule, current, limit)
                    elif current >= limit.value:
                        return _limit_message(rule, current, limit)
        except OverflowError as exc:
            return str(exc)

        for rule, bucket in matched:
            if rule.measure == Measure.REQUEST:
                self._record(rule, bucket, 1.0, now)
            elif rule.aggregate == Aggregate.COUNT_DISTINCT:
                value = context.get(rule.measure.value, "")
                if value:
                    self._record_distinct(rule, bucket, value, now)
        return None

    def is_exhausted(self, model: str = "default") -> bool:
        """检查不按 IP 分组的金额和 Token 额度，不记录用量。"""
        now = time()
        context = self._context(model=model)
        for rule in self.rules:
            if Dimension.IP in rule.group_by or rule.measure not in {
                Measure.COST, Measure.TOKEN, Measure.TOKEN_IN, Measure.TOKEN_OUT,
            }:
                continue
            try:
                bucket = self._get_bucket(rule, context)
            except OverflowError:
                return True
            if bucket is not None and any(self._read(rule, bucket, limit, now) >= limit.value for limit in rule.limits):
                return True
        return False

    def record_usage(self, model: str, cost: float = 0.0, tokens: int = 0,
                     tokens_in: int = 0, tokens_out: int = 0, client_ip: str = ""):
        now = time()
        context = self._context(model, client_ip)
        amounts = {
            Measure.COST: float(cost or 0),
            Measure.TOKEN: float(tokens or 0),
            Measure.TOKEN_IN: float(tokens_in or 0),
            Measure.TOKEN_OUT: float(tokens_out or 0),
        }
        for rule in self.rules:
            amount = amounts.get(rule.measure, 0.0)
            if amount <= 0:
                continue
            try:
                bucket = self._get_bucket(rule, context)
            except OverflowError:
                logger.error("[quota] Bucket limit reached while recording rule %s", rule.id)
                continue
            if bucket is not None:
                self._record(rule, bucket, amount, now)

    def get_status(self) -> dict:
        now = time()
        result = {}
        for rule in self.rules:
            buckets = self._rule_buckets(rule.id)
            if not buckets and not rule.group_by and not rule.where:
                bucket = self._get_bucket(rule, self._context())
                buckets = [bucket] if bucket is not None else []
            statuses = self._aggregate_rule_status(rule, buckets, now)
            result[f"rule:{rule.id}"] = {
                **_primary_status(statuses),
                **rule.to_dict(),
                "limits": statuses,
                "subjects": len(buckets),
            }
        return result

    def get_ip_breakdown(self, client_ip: str) -> List[dict]:
        """返回一个 IP 的实时规则和各模型组合桶状态。"""
        if not client_ip:
            return []
        now = time()
        output = []
        for rule in self.rules:
            groups_by_ip = Dimension.IP in rule.group_by
            ip_filter = rule.where.get(Dimension.IP.value)
            if ip_filter and not _ip_matches(ip_filter, client_ip):
                continue
            filters_by_ip = bool(ip_filter)
            if not groups_by_ip and not filters_by_ip:
                continue
            rule_buckets = self._rule_buckets(rule.id)
            matching = (
                [bucket for bucket in rule_buckets if bucket.dimensions.get("ip") == client_ip]
                if groups_by_ip else rule_buckets
            )
            if not matching and rule.group_by == (Dimension.IP,) and self._matches(rule, self._context(client_ip=client_ip)):
                matching = [_Bucket(dimensions={"ip": client_ip})]
            elif not matching and not rule.group_by and filters_by_ip and Dimension.MODEL.value not in rule.where:
                matching = [_Bucket(dimensions={})]
            buckets = []
            for bucket in matching:
                statuses = [self._status(rule, bucket, limit, now) for limit in rule.limits]
                buckets.append({
                    "dimensions": dict(bucket.dimensions),
                    **_primary_status(statuses),
                    "limits": statuses,
                })
            output.append({**rule.to_dict(), "buckets": buckets})
        return output

    def get_ip_summary(self, client_ip: str) -> Optional[dict]:
        breakdown = self.get_ip_breakdown(client_ip)
        limits = [limit for rule in breakdown for bucket in rule["buckets"] for limit in bucket["limits"]]
        if not limits:
            if breakdown:
                return {
                    "remaining_ratio": 1.0,
                    "exhausted": False,
                    "rule_count": len(breakdown),
                    "bucket_count": 0,
                    "label": "",
                    "measure": "",
                    "current": 0,
                    "limit": 0,
                    "remaining": 0,
                }
            return None
        worst = min(limits, key=lambda item: item.get("remaining_ratio", 1.0))
        return {
            "remaining_ratio": worst.get("remaining_ratio", 1.0),
            "exhausted": any(item.get("remaining", 0) <= 0 for item in limits),
            "rule_count": len([rule for rule in breakdown if rule["buckets"]]),
            "bucket_count": sum(len(rule["buckets"]) for rule in breakdown),
            "label": worst.get("label", ""),
            "measure": worst.get("measure", ""),
            "current": worst.get("current", 0),
            "limit": worst.get("limit", 0),
            "remaining": worst.get("remaining", 0),
        }

    def reset_rule(self, rule_id: str) -> dict:
        if rule_id not in self._rules:
            raise ValueError(f"Unknown quota rule: {rule_id}")
        keys = [key for key in self._buckets if key[0] == rule_id]
        for key in keys:
            self._buckets.pop(key, None)
        self._snapshot_dirty = True
        return {"status_key": f"rule:{rule_id}", "rule_id": rule_id, "affected": len(keys)}

    def _rule_buckets(self, rule_id: str) -> List[_Bucket]:
        return [bucket for (stored_rule_id, _), bucket in self._buckets.items() if stored_rule_id == rule_id]

    def _record(self, rule: DimensionalRule, bucket: _Bucket, amount: float, now: float):
        bucket.last_seen = now
        sliding_recorded = False
        fixed_periods = set()
        for limit in rule.limits:
            if limit.window == WindowType.SLIDING:
                if sliding_recorded:
                    continue
                if rule.aggregate == Aggregate.COUNT:
                    bucket.events.append(now)
                else:
                    bucket.amounts.append((now, amount))
                    bucket.permanent_amount += amount
                sliding_recorded = True
            elif limit.period not in fixed_periods:
                window = bucket.fixed.setdefault(limit.period, _FixedValue())
                old_start = window.start
                window.add(now, limit.period, amount)
                if window.start != old_start:
                    self._snapshot_dirty = True
                fixed_periods.add(limit.period)

    def _record_distinct(self, rule: DimensionalRule, bucket: _Bucket, value: str, now: float):
        bucket.last_seen = now
        bucket.distinct_seen[value] = now
        for limit in rule.limits:
            if limit.window == WindowType.FIXED:
                window = bucket.distinct_fixed.setdefault(limit.period, _FixedDistinct())
                old_start = window.start
                window.add(now, limit.period, value)
                if window.start != old_start:
                    self._snapshot_dirty = True

    def _read(self, rule: DimensionalRule, bucket: _Bucket, limit: Limit, now: float) -> float:
        if rule.aggregate == Aggregate.COUNT_DISTINCT:
            if limit.window == WindowType.FIXED:
                return bucket.distinct_fixed.setdefault(limit.period, _FixedDistinct()).count(now, limit.period)
            if math.isinf(limit.period):
                return len(bucket.distinct_seen)
            cutoff = now - limit.period
            return sum(1 for seen_at in bucket.distinct_seen.values() if seen_at > cutoff)
        if limit.window == WindowType.FIXED:
            return bucket.fixed.setdefault(limit.period, _FixedValue()).get(now, limit.period)
        if rule.aggregate == Aggregate.COUNT:
            if math.isinf(limit.period):
                return len(bucket.events)
            cutoff = now - limit.period
            return sum(1 for timestamp in bucket.events if timestamp > cutoff)
        if math.isinf(limit.period):
            return bucket.permanent_amount
        cutoff = now - limit.period
        return sum(amount for timestamp, amount in bucket.amounts if timestamp > cutoff)

    def _distinct_contains(self, bucket: _Bucket, limit: Limit, value: str, now: float) -> bool:
        if limit.window == WindowType.FIXED:
            return bucket.distinct_fixed.setdefault(limit.period, _FixedDistinct()).contains(now, limit.period, value)
        if math.isinf(limit.period):
            return value in bucket.distinct_seen
        return bucket.distinct_seen.get(value, float("-inf")) > now - limit.period

    def _status(self, rule: DimensionalRule, bucket: _Bucket, limit: Limit, now: float) -> dict:
        current = self._read(rule, bucket, limit, now)
        reset_at = None
        if limit.window == WindowType.FIXED:
            if rule.aggregate == Aggregate.COUNT_DISTINCT:
                reset_at = bucket.distinct_fixed.setdefault(limit.period, _FixedDistinct()).reset_at(limit.period)
            else:
                reset_at = bucket.fixed.setdefault(limit.period, _FixedValue()).reset_at(limit.period)
        return _status_payload(rule, current, limit, reset_at, now)

    def _aggregate_rule_status(self, rule: DimensionalRule, buckets: Iterable[_Bucket], now: float) -> List[dict]:
        bucket_list = list(buckets)
        statuses = []
        for limit in rule.limits:
            candidates = [self._status(rule, bucket, limit, now) for bucket in bucket_list]
            if candidates:
                statuses.append(min(candidates, key=lambda item: item["remaining_ratio"]))
            else:
                statuses.append(_status_payload(rule, 0, limit, None, now))
        return statuses

    def _cleanup(self, now: float):
        max_period_by_rule = {
            rule.id: max((limit.period for limit in rule.limits if not math.isinf(limit.period)), default=math.inf)
            for rule in self.rules
        }
        for key, bucket in list(self._buckets.items()):
            max_period = max_period_by_rule.get(key[0], math.inf)
            if not math.isinf(max_period):
                cutoff = now - max_period
                while bucket.events and bucket.events[0] <= cutoff:
                    bucket.events.popleft()
                while bucket.amounts and bucket.amounts[0][0] <= cutoff:
                    bucket.amounts.popleft()
                bucket.distinct_seen = {value: seen_at for value, seen_at in bucket.distinct_seen.items() if seen_at > cutoff}
                active_fixed = any(window.get(now, period) > 0 for period, window in bucket.fixed.items())
                active_distinct = any(window.count(now, period) > 0 for period, window in bucket.distinct_fixed.items())
                if not bucket.events and not bucket.amounts and not bucket.distinct_seen and not active_fixed and not active_distinct:
                    self._buckets.pop(key, None)

    def snapshot(self) -> dict:
        entries = []
        for (rule_id, _), bucket in self._buckets.items():
            fixed = [
                {"period": period, "value": window.value, "start": window.start}
                for period, window in bucket.fixed.items() if window.start > 0
            ]
            distinct_fixed = [
                {"period": period, "values": sorted(window.values), "start": window.start}
                for period, window in bucket.distinct_fixed.items() if window.start > 0
            ]
            if fixed or distinct_fixed:
                entries.append({"rule_id": rule_id, "dimensions": bucket.dimensions, "fixed": fixed, "distinct_fixed": distinct_fixed})
        return {"buckets": entries}

    def restore_snapshot(self, snapshot: dict) -> int:
        now = time()
        restored = 0
        for entry in (snapshot or {}).get("buckets", []):
            rule = self._rules.get(str(entry.get("rule_id") or ""))
            dimensions = entry.get("dimensions") or {}
            if not rule or not isinstance(dimensions, dict):
                continue
            expected_dimensions = {dimension.value for dimension in rule.group_by}
            if set(dimensions) != expected_dimensions or any(not str(value) for value in dimensions.values()):
                continue
            bucket_key = json.dumps(dimensions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            storage_key = (rule.id, bucket_key)
            bucket = self._buckets.get(storage_key)
            if bucket is None:
                if len(self._buckets) >= self.MAX_BUCKETS:
                    break
                bucket = _Bucket(dimensions={str(key): str(value) for key, value in dimensions.items()})
                self._buckets[storage_key] = bucket
            for item in entry.get("fixed", []):
                period = float(item.get("period") or 0)
                start = float(item.get("start") or 0)
                if period <= 0 or start <= 0 or (not math.isinf(period) and now >= start + period):
                    continue
                window = bucket.fixed.setdefault(period, _FixedValue())
                window.value = float(item.get("value") or 0)
                window.start = start
                restored += 1
            for item in entry.get("distinct_fixed", []):
                period = float(item.get("period") or 0)
                start = float(item.get("start") or 0)
                if period <= 0 or start <= 0 or (not math.isinf(period) and now >= start + period):
                    continue
                window = bucket.distinct_fixed.setdefault(period, _FixedDistinct())
                window.values = {str(value) for value in (item.get("values") or [])}
                window.start = start
                restored += 1
        return restored


def _text_matches(pattern: str, value: str) -> bool:
    if pattern in ("", "*", "all"):
        return True
    return fnmatchcase(value, pattern) if any(char in pattern for char in "*?[") else value == pattern


def _ip_matches(pattern: str, value: str) -> bool:
    if pattern in ("", "*", "all"):
        return True
    if not value:
        return False
    if "/" in pattern:
        try:
            return ipaddress.ip_address(value) in ipaddress.ip_network(pattern, strict=False)
        except ValueError:
            return False
    return _text_matches(pattern, value)


def _period_text(period: float) -> str:
    if math.isinf(period):
        return "inf"
    if period >= 86400 and period % 86400 == 0:
        return f"{int(period / 86400)}d"
    if period >= 3600 and period % 3600 == 0:
        return f"{int(period / 3600)}h"
    if period >= 60 and period % 60 == 0:
        return f"{int(period / 60)}min"
    return f"{int(period)}s"


def _status_payload(rule: DimensionalRule, current: float, limit: Limit,
                    reset_at: Optional[float], now: float) -> dict:
    remaining = max(0.0, limit.value - current)
    payload = {
        "measure": rule.measure.value,
        "aggregate": rule.aggregate.value,
        "current": round(current, 4),
        "limit": limit.value,
        "remaining": round(remaining, 4),
        "remaining_ratio": max(0.0, min(1.0, remaining / limit.value)) if limit.value > 0 else 0.0,
        "period": limit.period if not math.isinf(limit.period) else "inf",
        "window": limit.window.value,
        "label": f"{limit.value:g}/{_period_text(limit.period)}",
    }
    if reset_at is not None and reset_at > now:
        payload["reset_at"] = round(reset_at, 3)
        payload["reset_in"] = round(reset_at - now, 3)
    return payload


def _primary_status(statuses: List[dict]) -> dict:
    if not statuses:
        return {"current": 0, "limit": 0, "remaining": 0, "remaining_ratio": 1.0}
    worst = min(statuses, key=lambda item: item.get("remaining_ratio", 1.0))
    return {
        "current": worst.get("current", 0),
        "limit": worst.get("limit", 0),
        "remaining": worst.get("remaining", 0),
        "remaining_ratio": worst.get("remaining_ratio", 1.0),
        "period": worst.get("period"),
        "window": worst.get("window"),
        "reset_at": worst.get("reset_at"),
        "label": worst.get("label", ""),
    }


def _limit_message(rule: DimensionalRule, current: float, limit: Limit) -> str:
    dimensions = "+".join(dimension.value for dimension in rule.group_by) or "key"
    return (
        f"Quota exceeded ({dimensions}, {rule.measure.value}/{rule.aggregate.value}: "
        f"{current:g}/{limit.value:g} per {_period_text(limit.period)})"
    )
