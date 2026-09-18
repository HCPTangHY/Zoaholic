"""Offline middleware regressions: python -B scripts/test_middleware_regressions.py.

Uses real request models and moderation route, but fakes authentication policy,
metrics, database writes and the upstream handler. No application startup or
network calls are performed. Run with the project's dependencies installed.
"""
import asyncio
from datetime import datetime, timezone
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DISABLE_DATABASE'] = 'true'

from fastapi import BackgroundTasks, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from core import middleware as m
from core.models import ModerationRequest
from routes import moderations as moderation_route

# Optional source override allows verifying that tests catch the pre-fix bugs.
if os.environ.get('ZOAHOLIC_MIDDLEWARE_TEST_TARGET'):
    spec = importlib.util.spec_from_file_location(
        'middleware_under_test', os.environ['ZOAHOLIC_MIDDLEWARE_TEST_TARGET'])
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

MISSING = object()


class MiddlewareRegressions(unittest.IsolatedAsyncioTestCase):
    def body(self, size=None):
        obj = {'model': 'audit-model', 'messages': [{'role': 'user', 'content': 'hello'}], 'padding': ''}
        raw = json.dumps(obj).encode()
        if size is not None:
            obj['padding'] = 'x' * (size - len(raw))
            raw = json.dumps(obj).encode()
            self.assertEqual(len(raw), size)
        return raw

    async def exercise(self, payload, *, retention=MISSING, moderation=False,
                       flagged=False, method='POST', content_type='application/json',
                       failure=None):
        preferences = {} if retention is MISSING else {'log_raw_data_retention_hours': retention}
        effective_retention = 24 if retention is MISSING or retention is None else retention
        api_keys = [{'api': 'unused-0'}, {'api': 'unused-1'},
                    {'api': 'audit-key', 'preferences': {'ENABLE_MODERATION': moderation}}]
        app = SimpleNamespace(state=SimpleNamespace(
            config={'api_keys': api_keys, 'preferences': preferences},
            api_list=[item['api'] for item in api_keys], api_keys_db=[]))
        scope = {'type': 'http', 'path': '/v1/chat/completions', 'method': method,
                 'headers': [(b'content-type', content_type.encode()),
                             (b'authorization', b'Bearer audit-key')],
                 'app': app, 'client': ('127.0.0.1', 1234)}
        sent, logs, threads, captured = [], [], [], []
        offset, upload_finished = 0, False
        disconnected = asyncio.Event()
        real_truncate, real_thread = m.truncate_for_logging, asyncio.to_thread
        ready = asyncio.Event()
        ready.set()

        async def receive():
            nonlocal offset, upload_finished
            if upload_finished:
                await disconnected.wait()
                return {'type': 'http.disconnect'}
            chunk = payload[offset:offset + 65536]
            offset += len(chunk)
            upload_finished = offset >= len(payload)
            return {'type': 'http.request', 'body': chunk, 'more_body': not upload_finished}

        async def send(message):
            sent.append(message)

        def truncate(value):
            logs.append(type(value))
            # Truncation must not mutate the parsed container used for validation.
            before = json.dumps(value) if isinstance(value, (dict, list)) else None
            result = real_truncate(value)
            if before is not None:
                self.assertEqual(before, json.dumps(value))
            return result

        async def to_thread(fn, *args, **kwargs):
            threads.append(fn)
            return await real_thread(fn, *args, **kwargs)

        async def upstream(request, api_index, background_tasks, *, endpoint):
            self.assertIsInstance(request, ModerationRequest)
            self.assertEqual(request.input, 'hello')
            self.assertEqual(api_index, 2)
            self.assertIsInstance(background_tasks, BackgroundTasks)
            self.assertEqual(endpoint, '/v1/moderations')
            raw = json.dumps({'results': [{'flagged': flagged}]})
            async def chunks():
                yield raw[:10].encode()
                yield raw[10:]
            return StreamingResponse(chunks())

        handler = SimpleNamespace(request_model=AsyncMock(side_effect=upstream))
        downstream_calls = []

        async def downstream(scope, receive, send):
            downstream_calls.append(True)
            frame = inspect.currentframe().f_back
            if method == 'POST' and 'application/json' in content_type:
                local = frame.f_locals
                self.assertEqual(local['body_chunks'], [])
                self.assertNotIn('message', local)
                for name in ('parsed_body', 'request_model', 'moderated_content'):
                    self.assertIsNone(local[name], name)
                del local
            del frame
            chunks = []
            while True:
                part = await receive()
                chunks.append(part.get('body', b''))
                if not part.get('more_body', False):
                    break
            self.assertEqual(b''.join(chunks), payload)
            if method == 'POST' and 'application/json' in content_type:
                self.assertEqual(inspect.getclosurevars(receive).nonlocals['body_bytes'], b'')
            next_receive = asyncio.create_task(receive())
            await asyncio.sleep(0)
            self.assertFalse(next_receive.done(), 'must wait for a real disconnect')
            disconnected.set()
            self.assertEqual((await next_receive)['type'], 'http.disconnect')
            info = m.request_info.get()
            captured.append(dict(info))
            if effective_retention > 0:
                self.assertNotIn('audit-key', info['request_headers'])
                remaining = (info['raw_data_expires_at'] - datetime.now(timezone.utc)).total_seconds()
                self.assertAlmostEqual(remaining, effective_retention * 3600, delta=10)
                if payload and method == 'POST' and 'application/json' in content_type:
                    self.assertLess(len(info['request_body']), 103000)
            else:
                for name in ('request_headers', 'request_body', 'raw_data_expires_at'):
                    self.assertIsNone(info[name], name)
            if failure == 'cancel':
                raise asyncio.CancelledError()
            if failure == 'error':
                raise RuntimeError('synthetic downstream failure')
            if failure == 'validation':
                class Example(BaseModel):
                    number: int
                Example(number='private-input')
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'data: one\n\n', 'more_body': True})
            await send({'type': 'http.response.body', 'body': b'data: two\n\n', 'more_body': False})

        middleware = m.StatsMiddleware(downstream, debug=False)
        middleware._dialect_prefixes = []
        sentinel = {'sentinel': True}
        token = m.request_info.set(sentinel)
        try:
            with patch.dict(sys.modules, {'main': SimpleNamespace(_db_ready=ready)}), \
                 patch.object(m, 'DISABLE_DATABASE', True), \
                 patch.object(m, 'is_global_ip_blocked', return_value=False), \
                 patch.object(m, 'is_key_ip_blocked', return_value=False), \
                 patch.object(m, 'on_request_start'), patch.object(m, 'on_request_end'), \
                 patch.object(m, 'enqueue_stats') as stats, \
                 patch.object(m, 'logger') as logger, \
                 patch.object(m, 'truncate_for_logging', side_effect=truncate), \
                 patch.object(m.asyncio, 'to_thread', side_effect=to_thread), \
                 patch.object(moderation_route, 'get_model_handler', return_value=handler), \
                 patch.object(moderation_route, 'moderations', wraps=moderation_route.moderations) as route:
                if failure == 'cancel':
                    with self.assertRaises(asyncio.CancelledError):
                        await middleware(scope, receive, send)
                else:
                    await middleware(scope, receive, send)
                self.assertIs(m.request_info.get(), sentinel)
                if moderation:
                    route.assert_awaited_once()
                    request = route.await_args.kwargs['http_request']
                    self.assertIsInstance(request, Request)
                    self.assertIs(request.app, app)
                    handler.request_model.assert_awaited_once()
                else:
                    handler.request_model.assert_not_called()
                if flagged:
                    self.assertFalse(downstream_calls)
                    self.assertEqual(sent[0]['status'], 400)
                    self.assertTrue(stats.call_args.args[0]['is_flagged'])
                elif failure != 'cancel':
                    self.assertTrue(downstream_calls)
                    expected = {'error': 500, 'validation': 422}.get(failure, 200)
                    self.assertEqual(sent[0]['status'], expected)
                    if failure == 'validation':
                        self.assertNotIn('private-input', str(logger.error.call_args))
                    elif failure is None:
                        self.assertEqual(b''.join(x.get('body', b'') for x in sent),
                                         b'data: one\n\ndata: two\n\n')
        finally:
            m.request_info.reset(token)
        return logs, threads, captured

    async def test_default_retention(self):
        logs, _, _ = await self.exercise(self.body())
        self.assertEqual(logs, [dict])

    async def test_null_retention_defaults_to_24h(self):
        await self.exercise(self.body(), retention=None)

    async def test_zero_retention_disables_raw_logs(self):
        logs, _, _ = await self.exercise(self.body(9 * 1024 * 1024), retention=0)
        self.assertEqual(logs, [])

    async def test_positive_retention(self):
        await self.exercise(self.body(), retention=2)

    async def test_moderation_route_allowed(self):
        await self.exercise(self.body(), moderation=True)

    async def test_moderation_route_rejected(self):
        await self.exercise(self.body(), moderation=True, flagged=True)

    async def test_moderation_with_raw_logging_disabled(self):
        await self.exercise(self.body(), moderation=True, retention=0)

    async def test_threshold_below(self):
        _, threads, _ = await self.exercise(self.body(8 * 1024 * 1024 - 1))
        self.assertNotIn(json.loads, threads)

    async def test_threshold_at(self):
        _, threads, _ = await self.exercise(self.body(8 * 1024 * 1024))
        self.assertIn(json.loads, threads)

    async def test_threshold_above(self):
        _, threads, _ = await self.exercise(self.body(8 * 1024 * 1024 + 1))
        self.assertIn(json.loads, threads)

    async def test_malformed_json(self):
        await self.exercise(b'{invalid json')

    async def test_invalid_utf8(self):
        await self.exercise(b'\xff invalid utf8')

    async def test_empty_json(self):
        await self.exercise(b'')

    async def test_json_array(self):
        logs, _, _ = await self.exercise(b'[{"text":"hello"}]')
        self.assertEqual(logs, [list])

    async def test_non_json_passthrough(self):
        await self.exercise(b'multipart body', content_type='multipart/form-data')

    async def test_get_passthrough(self):
        await self.exercise(b'', method='GET')

    async def test_cancel_resets_context(self):
        await self.exercise(self.body(), failure='cancel')

    async def test_exception_resets_context(self):
        await self.exercise(self.body(), failure='error')

    async def test_validation_error_omits_input(self):
        await self.exercise(self.body(), failure='validation')


if __name__ == '__main__':
    unittest.main(verbosity=2)
