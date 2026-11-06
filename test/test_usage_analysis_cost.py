import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import core.stats as core_stats
import db
from core.d1_client import normalize_d1_params
from routes import stats as stats_route


@pytest.mark.parametrize("db_type", ["sqlite", "d1"])
def test_usage_analysis_recorded_cost_follows_filters(monkeypatch, tmp_path, db_type):
    async def run():
        database_path = tmp_path / "usage.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
        session_factory = async_sessionmaker(engine)

        @asynccontextmanager
        async def session_scope():
            async with session_factory() as session:
                yield session

        class LocalD1:
            async def query_all(self, sql, params=None):
                with sqlite3.connect(database_path) as connection:
                    connection.row_factory = sqlite3.Row
                    return [dict(row) for row in connection.execute(sql, normalize_d1_params(params))]

        monkeypatch.setattr(stats_route, "DISABLE_DATABASE", False)
        monkeypatch.setattr(stats_route, "DB_TYPE", db_type)
        monkeypatch.setattr(stats_route, "async_session_scope", session_scope)
        monkeypatch.setattr(db, "d1_client", LocalD1())
        monkeypatch.setattr(stats_route, "get_app", lambda: SimpleNamespace())
        # 当前报价与历史价格不同，筛选费用仍应取历史记录（含折算后的缓存输入价）。
        monkeypatch.setattr(core_stats, "get_current_model_prices", lambda *args, **kwargs: (999, 999, 999))
        inside = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
        before = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        after = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        samples = [
            ("channel-a", "model-a", inside, 1_000_000, 100_000, 1, 5),
            ("channel-a", "model-a", inside, 1_000_000, 0, 0.5, 5),
            ("channel-a", "model-b", inside, 1_000_000, 0, 3, 5),
            ("channel-b", "model-a", inside, 1_000_000, 0, 5, 5),
            ("channel-a", "model-a", before, 1_000_000, 0, 11, 5),
            ("channel-a", "model-a", after, 1_000_000, 0, 13, 5),
            ("channel-c", "model-c", inside, 100, 100, None, None),
        ]
        rows = [dict(provider=p, model=m, timestamp=t, prompt_tokens=pt, completion_tokens=ct,
                     total_tokens=pt + ct, prompt_price=pp, completion_price=cp)
                for p, m, t, pt, ct, pp, cp in samples]
        try:
            async with engine.begin() as connection:
                await connection.run_sync(db.RequestStat.__table__.create)
                await connection.execute(db.RequestStat.__table__.insert(), rows)

            async def query(provider=None, model=None):
                response = await stats_route.get_usage_analysis(
                    request=None, token="test-admin", hours=24,
                    start_datetime="2026-10-05T00:00:00Z", end_datetime="2026-10-07T00:00:00Z",
                    provider=provider, model=model,
                )
                return json.loads(response.body)["data"]

            data = await query("channel-a", "model-a")
            assert len(data) == 1
            assert data[0]["recorded_cost"] == pytest.approx(2)
            assert data[0]["request_count"] == 2
            assert data[0]["total_cost"] != data[0]["recorded_cost"]
            for provider, model, expected in [
                (None, None, 10), ("channel-a", None, 5), (None, "model-a", 7),
                ("channel-a,channel-b", "model-a,model-b", 10), ("channel-c", "model-c", 0),
            ]:
                data = await query(provider, model)
                assert sum(row["recorded_cost"] for row in data) == pytest.approx(expected)
            assert await query("missing", "model-a") == []
        finally:
            await engine.dispose()

    asyncio.run(run())
