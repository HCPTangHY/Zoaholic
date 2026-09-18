from core.quota.dimensional import DimensionalQuotaCounter, parse_dimensional_rules
from core.quota.runtime import QuotaRegistry


def _rules(raw):
    rules = parse_dimensional_rules(raw)
    assert len(rules) == len(raw)
    return rules


def test_ip_model_cross_product_has_independent_buckets(monkeypatch):
    import core.quota.dimensional as dimensional

    now = [1000.0]
    monkeypatch.setattr(dimensional, "time", lambda: now[0])
    counter = DimensionalQuotaCounter(_rules([{
        "id": "ip_model_requests",
        "group_by": ["ip", "model"],
        "measure": "request",
        "aggregate": "count",
        "limit": "2/min",
    }]))

    assert counter.check_request("gpt-5.6", "10.0.0.1") is None
    assert counter.check_request("gpt-5.6", "10.0.0.1") is None
    assert "Quota exceeded" in counter.check_request("gpt-5.6", "10.0.0.1")

    assert counter.check_request("gpt-5.6-terra", "10.0.0.1") is None
    assert counter.check_request("gpt-5.6", "10.0.0.2") is None

    breakdown = counter.get_ip_breakdown("10.0.0.1")
    assert len(breakdown) == 1
    buckets = breakdown[0]["buckets"]
    assert {bucket["dimensions"]["model"] for bucket in buckets} == {"gpt-5.6", "gpt-5.6-terra"}
    assert max(bucket["current"] for bucket in buckets) == 2


def test_filters_and_token_measure_are_independent(monkeypatch):
    import core.quota.dimensional as dimensional

    monkeypatch.setattr(dimensional, "time", lambda: 1000.0)
    counter = DimensionalQuotaCounter(_rules([{
        "id": "gpt_tokens_by_ip",
        "group_by": ["ip"],
        "where": {"model": "gpt-5.6*"},
        "measure": "token",
        "aggregate": "sum",
        "limit": "100/day:fixed",
    }]))

    assert counter.check_request("claude-opus", "10.0.0.1") is None
    counter.record_usage("claude-opus", tokens=90, client_ip="10.0.0.1")
    assert counter.get_ip_breakdown("10.0.0.1")[0]["buckets"] == []

    assert counter.check_request("gpt-5.6", "10.0.0.1") is None
    counter.record_usage("gpt-5.6", tokens=60, client_ip="10.0.0.1")
    breakdown = counter.get_ip_breakdown("10.0.0.1")
    assert breakdown[0]["buckets"][0]["current"] == 60
    assert counter.check_request("gpt-5.6", "10.0.0.1") is None
    counter.record_usage("gpt-5.6", tokens=50, client_ip="10.0.0.1")
    assert "Quota exceeded" in counter.check_request("gpt-5.6", "10.0.0.1")


def test_count_distinct_ip_can_group_by_model(monkeypatch):
    import core.quota.dimensional as dimensional

    monkeypatch.setattr(dimensional, "time", lambda: 1000.0)
    counter = DimensionalQuotaCounter(_rules([{
        "id": "unique_ips_per_model",
        "group_by": ["model"],
        "measure": "ip",
        "aggregate": "count_distinct",
        "limit": "2/day",
    }]))

    assert counter.check_request("gpt-5.6", "10.0.0.1") is None
    assert counter.check_request("gpt-5.6", "10.0.0.1") is None
    assert counter.check_request("gpt-5.6", "10.0.0.2") is None
    assert "Quota exceeded" in counter.check_request("gpt-5.6", "10.0.0.3")
    assert counter.check_request("gpt-5.6-terra", "10.0.0.3") is None


def test_registry_merges_legacy_and_dimensional_ip_quota(monkeypatch, tmp_path):
    import core.quota.runtime as runtime
    import core.quota.dimensional as dimensional

    monkeypatch.setattr(runtime, "_SNAPSHOT_PATH", str(tmp_path / "quota.json"))
    monkeypatch.setattr(runtime, "time", lambda: 1000.0)
    monkeypatch.setattr(dimensional, "time", lambda: 1000.0)

    registry = QuotaRegistry()
    registry.init_from_config({
        "api_keys": [{
            "api": "test-key",
            "preferences": {
                "quota": {"ip:request": "10/min"},
                "quota_rules": [{
                    "id": "ip_model_cost",
                    "group_by": ["ip", "model"],
                    "measure": "cost",
                    "aggregate": "sum",
                    "limit": "5/day:fixed",
                }],
            },
        }],
    })

    assert registry.check("test-key", "gpt-5.6", "10.0.0.1") is None
    registry.record_usage("test-key", "gpt-5.6", cost=2, client_ip="10.0.0.1")

    status = registry.get_key_status("test-key")
    assert "ip:request:default" in status
    assert "rule:ip_model_cost" in status

    breakdown = registry.get_ip_quota_breakdown("test-key", "10.0.0.1")
    assert {rule["id"] for rule in breakdown} == {"legacy_ip_request_default", "ip_model_cost"}
    summary = registry.get_ip_quota_summary("test-key", "10.0.0.1")
    assert summary is not None
    assert summary["rule_count"] == 2
    assert summary["remaining_ratio"] == 0.6


def test_legacy_ip_breakdown_keeps_each_ip_and_all_windows(monkeypatch, tmp_path):
    import core.quota.runtime as runtime

    monkeypatch.setattr(runtime, "_SNAPSHOT_PATH", str(tmp_path / "quota.json"))
    monkeypatch.setattr(runtime, "time", lambda: 100000.0)
    registry = QuotaRegistry()
    registry.init_from_config({
        "api_keys": [{
            "api": "legacy-key",
            "preferences": {"quota": {"ip:request": "10/min,100/day"}},
        }],
    })

    assert registry.check("legacy-key", "model", "10.0.0.1") is None
    assert registry.check("legacy-key", "model", "10.0.0.1") is None
    assert registry.check("legacy-key", "model", "10.0.0.2") is None

    first = registry.get_ip_quota_breakdown("legacy-key", "10.0.0.1")[0]["buckets"][0]
    second = registry.get_ip_quota_breakdown("legacy-key", "10.0.0.2")[0]["buckets"][0]
    assert first["current"] == 2
    assert second["current"] == 1
    assert len(first["limits"]) == 2
    assert {limit["limit"] for limit in first["limits"]} == {10.0, 100.0}


def test_dimensional_fixed_window_snapshot_roundtrip(monkeypatch):
    import core.quota.dimensional as dimensional

    now = [1000.0]
    monkeypatch.setattr(dimensional, "time", lambda: now[0])
    rules = _rules([{
        "id": "fixed_requests",
        "group_by": ["ip", "model"],
        "measure": "request",
        "limit": "3/hour:fixed",
    }])
    original = DimensionalQuotaCounter(rules)
    assert original.check_request("gpt-5.6", "10.0.0.1") is None
    assert original.check_request("gpt-5.6", "10.0.0.1") is None

    restored = DimensionalQuotaCounter(rules)
    assert restored.restore_snapshot(original.snapshot()) == 1
    breakdown = restored.get_ip_breakdown("10.0.0.1")
    assert breakdown[0]["buckets"][0]["current"] == 2
    assert restored.check_request("gpt-5.6", "10.0.0.1") is None
    assert "Quota exceeded" in restored.check_request("gpt-5.6", "10.0.0.1")
