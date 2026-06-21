import sys
from pathlib import Path

ROOT = next(
    parent for parent in Path(__file__).resolve().parents
    if (parent / "core").is_dir() and (parent / "routes").is_dir()
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_fixed_quota_status_exposes_reset_time(monkeypatch):
    from core.quota.parser import parse_config
    import core.quota.runtime as runtime

    now = [1000.0]
    monkeypatch.setattr(runtime, "time", lambda: now[0])

    counter = runtime.QuotaCounter(parse_config({"request": "2/5h:fixed"}))

    initial = counter.get_status()["key:request:default"]
    assert initial["window"] == "fixed"
    assert "reset_at" not in initial
    assert "reset_in" not in initial

    counter.check_request(model="default", client_ip="127.0.0.1")
    status = counter.get_status()["key:request:default"]

    assert status["window"] == "fixed"
    assert status["reset_at"] == 19000.0
    assert status["reset_in"] == 18000.0

    now[0] = 19000.0
    expired = counter.get_status()["key:request:default"]
    assert expired["current"] == 0
    assert "reset_at" not in expired
    assert "reset_in" not in expired


def test_sliding_quota_status_keeps_reset_time_empty(monkeypatch):
    from core.quota.parser import parse_config
    import core.quota.runtime as runtime

    now = [1000.0]
    monkeypatch.setattr(runtime, "time", lambda: now[0])

    counter = runtime.QuotaCounter(parse_config({"request": "2/5h"}))
    counter.check_request(model="default", client_ip="127.0.0.1")
    status = counter.get_status()["key:request:default"]

    assert status["window"] == "sliding"
    assert "reset_at" not in status
    assert "reset_in" not in status
