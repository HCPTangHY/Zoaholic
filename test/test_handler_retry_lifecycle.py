"""Offline regressions for bounded routing and release of exhausted requests.

Exercise ModelRequestHandler.request_model itself; no production configuration,
network requests, database writes, or service process are used.
"""
import asyncio
import gc
import json
import sys
import weakref
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.handler as handler_module
from core.handler import ModelRequestHandler
from core.models import RequestModel

MODEL = "gpt-image-2"


class Pool:
    def __init__(self, slots=1, limited=False):
        self.slots = slots
        self.limited = limited
        self.checked_models = []
        self.requests = {}

    def get_enabled_items_count(self):
        return self.slots

    async def is_all_rate_limited(self, model):
        self.checked_models.append(model)
        # Keep even the unfixed busy loop cancellable during the regression test.
        await asyncio.sleep(0)
        return model in self.limited if isinstance(self.limited, set) else self.limited

    async def after_next_current(self):
        return "fake-upstream-key"


def provider(name, priority=0, upstream=None):
    result = {
        "provider": name,
        "api": ["fake-upstream-key"],
        "engine": "openai",
        "base_url": "https://example.invalid/v1",
        "_model_dict_cache": {MODEL: upstream or MODEL},
        "preferences": {"key_rules": [{"match": "default", "duration": 0}]},
    }
    if priority is not None:
        result.update(_virtual_route_provider=True, _virtual_priority=priority)
    return result


def request():
    return RequestModel(model=MODEL, messages=[{"role": "user", "content": "draw a tree"}])


@pytest.fixture
def make_case(monkeypatch):
    def build(providers, pools, *, cooldown=0, auto_retry=True, route=None, process=None):
        info = {"model": MODEL, "api_key": "fake-client-key"}
        stats = []
        attempts = []

        async def fake_process(data, chosen, *args, **kwargs):
            attempts.append(chosen["provider"])
            if process:
                return await process(data, chosen)
            return JSONResponse({"ok": True})

        async def passthrough_interceptor(data, *args, **kwargs):
            return data

        async def fast_sleep(delay):
            await asyncio.sleep(0)

        app = SimpleNamespace(state=SimpleNamespace(
            config={
                "providers": providers,
                "api_keys": [{"api": "fake-client-key", "model": [MODEL],
                              "preferences": {"AUTO_RETRY": auto_retry}}],
            },
            api_list=["fake-client-key"],
            user_api_keys_rate_limit={"fake-client-key": SimpleNamespace(next=AsyncMock())},
            provider_timeouts={"global": {"default": 600}},
            keepalive_interval={"global": {"default": 15}},
            channel_manager=SimpleNamespace(cooldown_period=cooldown, exclude_model=AsyncMock()),
        ))
        router = AsyncMock(side_effect=route) if route else AsyncMock(return_value=providers)
        monkeypatch.setattr(handler_module, "get_right_order_providers", router)
        monkeypatch.setattr(handler_module, "provider_api_circular_list", pools)
        monkeypatch.setattr(handler_module, "process_request", fake_process)
        monkeypatch.setattr(handler_module, "enqueue_stats", lambda data, **kwargs: stats.append(dict(data)))
        # Avoid retry backoff in tests without replacing asyncio.sleep globally.
        monkeypatch.setattr(handler_module, "asyncio", SimpleNamespace(
            sleep=fast_sleep, Lock=asyncio.Lock, CancelledError=asyncio.CancelledError,
        ))
        monkeypatch.setattr("core.plugins.interceptors.apply_inbound_interceptors", passthrough_interceptor)
        monkeypatch.setattr("core.plugins.interceptors.apply_channel_inbound_interceptors", passthrough_interceptor)
        handler = ModelRequestHandler(app, lambda: info, lambda *args, **kwargs: None)
        return SimpleNamespace(handler=handler, app=app, info=info, stats=stats,
                               attempts=attempts, pools=pools, router=router)
    return build


@pytest.mark.asyncio
@pytest.mark.parametrize("priorities", [[0, 1, 2, 3], [0, 0, 1, 1], [0, 0], [None, None]])
async def test_all_channels_limited_terminate_and_record_failure(make_case, priorities):
    providers = [provider(f"p{i}", p) for i, p in enumerate(priorities)]
    pools = {p["provider"]: Pool(limited=True) for p in providers}
    case = make_case(providers, pools)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code >= 400
    assert "rate limited" in json.loads(response.body)["error"]["message"]
    assert case.attempts == []
    assert len(case.stats) == 1
    assert case.info["success"] is False
    assert sum(len(p.checked_models) for p in pools.values()) <= 100


@pytest.mark.asyncio
async def test_limited_channels_do_not_clone_or_run_channel_plugins(make_case, monkeypatch):
    case = make_case([provider("p0"), provider("p1", 1)],
                     {"p0": Pool(limited=True), "p1": Pool(limited=True)})
    clone = Mock(wraps=handler_module._clone_request_data_for_channel_attempt)
    inbound = AsyncMock(side_effect=lambda data, *args: data)
    monkeypatch.setattr(handler_module, "_clone_request_data_for_channel_attempt", clone)
    monkeypatch.setattr("core.plugins.interceptors.apply_channel_inbound_interceptors", inbound)
    await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    clone.assert_not_called()
    inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_priority_checks_each_channels_own_model(make_case):
    providers = [provider("p0", 0, "image-a"), provider("p1", 0, "image-b"), provider("fallback", 1)]
    pools = {"p0": Pool(limited=True), "p1": Pool(limited={"image-a"}), "fallback": Pool()}
    case = make_case(providers, pools)
    response = await case.handler.request_model(request(), 0, None)
    assert response.status_code == 200
    assert case.attempts == ["p1"]
    assert set(pools["p1"].checked_models) == {"image-b"}


@pytest.mark.asyncio
async def test_same_priority_key_retry_precedes_fallback(make_case):
    calls = Counter()

    async def process(data, chosen):
        name = chosen["provider"]
        calls[name] += 1
        if name == "p0" and calls[name] == 1:
            raise HTTPException(503, "transient failure")
        return JSONResponse({"ok": True})

    case = make_case([provider("p0", 0), provider("fallback", 1)],
                     {"p0": Pool(slots=2), "fallback": Pool()}, process=process)
    response = await case.handler.request_model(request(), 0, None)
    assert response.status_code == 200
    assert case.attempts == ["p0", "p0"]


@pytest.mark.asyncio
async def test_failed_key_budget_downgrades_even_if_cooldown_expires(make_case):
    async def process(data, chosen):
        if chosen["provider"] != "fallback":
            raise HTTPException(503, "temporary error with immediately recovered key")
        return JSONResponse({"ok": True})

    case = make_case([provider("p0"), provider("fallback", 1)],
                     {"p0": Pool(slots=2), "fallback": Pool()}, process=process)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code == 200
    assert case.attempts == ["p0", "p0", "fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_retry,status", [(False, 503), (True, 400), (True, 429)])
async def test_disabled_or_nonretryable_image_failure_is_not_retried(make_case, auto_retry, status):
    async def fail(data, chosen):
        raise HTTPException(status, "upstream rejected request")

    case = make_case([provider("p0"), provider("p1", 1)], {"p0": Pool(), "p1": Pool()},
                     auto_retry=auto_retry, process=fail)
    kwargs = {} if auto_retry else {"override_providers": case.app.state.config["providers"]}
    response = await case.handler.request_model(request(), 0, None, **kwargs)
    assert response.status_code == status
    assert case.attempts == ["p0"]


@pytest.mark.asyncio
async def test_override_channel_test_still_skips_pool_preflight(make_case):
    providers = [provider("p0")]
    pool = Pool(limited=True)
    case = make_case(providers, {"p0": pool})
    response = await case.handler.request_model(request(), 0, None, override_providers=providers)
    assert response.status_code == 200
    assert case.attempts == ["p0"]
    assert pool.checked_models == []


@pytest.mark.asyncio
async def test_rebuilding_candidates_preserves_fallback_key_retries(make_case):
    providers = [provider("p0", 0), provider("p1", 1), provider("p2", 2)]
    # Earlier failures consume the original budget, but p2 still needs all three keys.
    candidates = [providers, providers[1:], providers[2:], providers[2:], providers[2:]]
    calls = Counter()

    async def process(data, chosen):
        name = chosen["provider"]
        calls[name] += 1
        if name != "p2" or calls[name] < 3:
            raise HTTPException(503, "unavailable")
        return JSONResponse({"ok": True})

    case = make_case(providers, {"p0": Pool(), "p1": Pool(), "p2": Pool(slots=3)},
                     cooldown=30, route=candidates, process=process)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code == 200
    assert case.attempts == ["p0", "p1", "p2", "p2", "p2"]


@pytest.mark.asyncio
async def test_rebuild_cannot_reset_global_attempt_limit(make_case):
    providers = [provider("p0"), provider("p1", 1)]
    pools = {"p0": Pool(), "p1": Pool()}
    expected_limit = len(providers) + 2 * len(providers)
    counter = 0

    async def route(*args, **kwargs):
        nonlocal counter
        counter += 1
        if counter == 1:
            return providers
        if counter > expected_limit + 2:
            pytest.fail("candidate rebuild reset the request's retry budget")
        # Alternate list lengths so the old cursor-based counter keeps resetting.
        fresh = [provider(f"fresh-{counter}-{i}", i) for i in range(2 + counter % 2)]
        pools.update({p["provider"]: Pool() for p in fresh})
        return fresh

    async def fail(data, chosen):
        raise HTTPException(503, "all requests fail")

    case = make_case(providers, pools, cooldown=30, route=route, process=fail)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code == 503
    assert len(case.attempts) <= expected_limit


@pytest.mark.asyncio
async def test_task_cancellation_during_preflight_is_not_retried(make_case):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    class WaitingPool(Pool):
        async def is_all_rate_limited(self, model):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

    case = make_case([provider("p0")], {"p0": WaitingPool()})
    task = asyncio.create_task(case.handler.request_model(request(), 0, None))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cleaned.is_set()
        assert case.attempts == []
        assert case.stats == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_exhausted_batch_releases_requests_and_finishes_tasks(make_case):
    case = make_case([provider("p0"), provider("p1", 1)],
                     {"p0": Pool(limited=True), "p1": Pool(limited=True)})
    before = asyncio.all_tasks()
    requests = [request() for _ in range(32)]
    refs = [weakref.ref(r) for r in requests]
    batch = asyncio.gather(*(case.handler.request_model(r, 0, None) for r in requests))
    results = await asyncio.wait_for(batch, 2)
    assert all(r.status_code >= 400 for r in results)
    del requests, batch
    await asyncio.sleep(0)
    gc.collect()  # Local test process only; production GC is not touched.
    assert all(ref() is None for ref in refs)
    assert not [t for t in asyncio.all_tasks() - before if not t.done()]


@pytest.mark.asyncio
async def test_retry_group_uses_each_channels_model_mapping(make_case):
    providers = [provider("p0", 0, "image-a"), provider("p1", 0, "image-b"), provider("fallback", 1)]
    calls = Counter()

    async def process(data, chosen):
        name = chosen["provider"]
        calls[name] += 1
        if name == "p0" and calls[name] == 2:
            return JSONResponse({"ok": True})
        raise HTTPException(503, "try another key")

    pools = {"p0": Pool(slots=2, limited={"image-b"}), "p1": Pool(), "fallback": Pool()}
    case = make_case(providers, pools, process=process)
    response = await case.handler.request_model(request(), 0, None)
    assert response.status_code == 200
    assert case.attempts == ["p0", "p1", "p0"]
    assert set(pools["p0"].checked_models) == {"image-a"}
    assert set(pools["p1"].checked_models) == {"image-b"}


@pytest.mark.asyncio
async def test_no_key_pool_channel_is_not_treated_as_exhausted(make_case):
    providers = [provider("p0"), provider("without-pool"), provider("fallback", 1)]
    case = make_case(providers, {"p0": Pool(limited=True), "fallback": Pool()})
    response = await case.handler.request_model(request(), 0, None)
    assert response.status_code == 200
    assert case.attempts == ["without-pool"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limited", [False, True])
async def test_absolute_limit_includes_skipped_and_failed_candidates(make_case, limited):
    providers = [provider(f"p{i}", None) for i in range(501)]
    pools = {p["provider"]: Pool(limited=limited) for p in providers}

    async def fail(data, chosen):
        raise HTTPException(503, "unavailable")

    case = make_case(providers, pools, process=fail)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 3)
    assert response.status_code >= 400
    assert sum(len(p.checked_models) for p in pools.values()) == 500
    assert len(case.attempts) == (0 if limited else 500)
    assert pools["p500"].checked_models == []


@pytest.mark.asyncio
async def test_same_length_candidate_rebuild_restarts_at_new_highest_priority(make_case):
    providers = [provider("old-high", 0), provider("fallback", 2)]
    new_providers = [provider("new-high", 1), providers[1]]

    async def process(data, chosen):
        if chosen["provider"] == "old-high":
            raise HTTPException(503, "old channel removed")
        return JSONResponse({"ok": True})

    pools = {name: Pool() for name in ("old-high", "new-high", "fallback")}
    case = make_case(providers, pools, cooldown=30, route=[providers, new_providers], process=process)
    response = await case.handler.request_model(request(), 0, None)
    assert response.status_code == 200
    assert case.attempts == ["old-high", "new-high"]


@pytest.mark.asyncio
@pytest.mark.parametrize('chain_size', [1, 3])
async def test_limited_virtual_chain_exits_to_regular_pool(make_case, chain_size):
    chain = [provider(f'v{i}', i) for i in range(chain_size)]
    regular = [provider('ordinary', None)]
    pools = {p['provider']: Pool(limited=True) for p in chain}
    pools['ordinary'] = Pool()

    async def route(*args, **kwargs):
        return regular if kwargs.get('skip_virtual') else chain

    case = make_case(chain, pools, route=route)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code == 200
    assert case.attempts == ['ordinary']
    assert sum(c.kwargs.get('skip_virtual', False) for c in case.router.await_args_list) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('cooldown', [0, 30])
async def test_regular_pool_gets_budget_after_small_virtual_chain(make_case, cooldown):
    chain = [provider('v')]
    regular = [provider(f'r{i}', None) for i in range(4)]
    pools = {p['provider']: Pool() for p in chain + regular}
    calls = 0

    async def route(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return chain
        return regular

    async def process(data, chosen):
        if chosen['provider'] != 'r3':
            raise HTTPException(503, 'unavailable')
        return JSONResponse({'ok': True})

    case = make_case(chain, pools, route=route, cooldown=cooldown, process=process)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code == 200
    assert case.attempts == ['v', 'r0', 'r1', 'r2', 'r3']
    if cooldown:
        assert all(c.kwargs.get('skip_virtual') for c in case.router.await_args_list[2:])


@pytest.mark.asyncio
async def test_regular_fallback_empty_records_failure_once(make_case):
    async def route(*args, **kwargs):
        if kwargs.get('skip_virtual'):
            raise HTTPException(404, 'no authorized ordinary model')
        return [provider('v')]

    case = make_case([provider('v')], {'v': Pool(limited=True)}, route=route)
    response = await asyncio.wait_for(case.handler.request_model(request(), 0, None), 1)
    assert response.status_code >= 400
    assert len(case.stats) == 1
    assert case.router.await_count == 2


@pytest.mark.asyncio
async def test_matching_cold_virtual_chain_uses_authorized_regular_pool(monkeypatch):
    import core.routing as routing
    chain = [provider('v')]
    regular = [provider('r', None)]
    config = {'api_keys': [{'model': [MODEL]}]}
    app = SimpleNamespace(state=SimpleNamespace(channel_manager=SimpleNamespace(
        get_available_providers=AsyncMock(return_value=[]))))
    monkeypatch.setattr('core.virtual_routing.resolve_virtual_model', lambda *args: chain)
    rules = AsyncMock(return_value=['r/' + MODEL])
    monkeypatch.setattr(routing, 'get_provider_rules', rules)
    monkeypatch.setattr(routing, 'get_provider_list', lambda *args: regular)
    assert await routing.get_matching_providers(MODEL, config, 0, app) == regular
    config['api_keys'][0]['model'] = ['different-model']
    rules.reset_mock()
    assert await routing.get_matching_providers(MODEL, config, 0, app, skip_virtual=True) == []
    rules.assert_not_awaited()
