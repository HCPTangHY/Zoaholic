"""Offline regressions for the actual Responses route and request conversion chain.

Run: python -B scripts/test_responses_integration.py
No application startup, real credentials, database writes or network calls.
"""
import asyncio
from contextlib import asynccontextmanager
import importlib
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

from fastapi import BackgroundTasks, Request, FastAPI
from starlette.responses import Response
from core.dialects import get_dialect
from core.dialects.openai_responses import register, render_responses_response
from core.dialects.responses_stream import render_responses_iterator
from core.dialects.router import _create_custom_handler_wrapper, _create_generic_handler
from core.stream_errors import UpstreamStreamError
from core.models import RequestModel

process_module = importlib.import_module('core.process_request')


def canonical(text='hello'):
    return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}


def sse(data):
    return 'data: ' + json.dumps(data, ensure_ascii=False) + '\n\n'


class Source:
    def __init__(self, values):
        self.values = iter(values)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            value = next(self.values)
        except StopIteration:
            raise StopAsyncIteration
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self):
        self.closed = True


class ResponsesIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def process(self, *, stream=False, mode='auto', values=None):
        if values is None:
            values = [sse({'choices': [{'index': 0, 'delta': {'content': 'hello'},
                                      'finish_reason': 'stop'}]}), 'data: [DONE]\n\n'] if stream else [canonical()]
        source = Source(values)

        @asynccontextmanager
        async def client(*args):
            yield object()

        app = FastAPI()
        app.state.config = {}
        app.state.error_triggers = []
        app.state.client_manager = SimpleNamespace(get_client=client)
        info = {'request_id': 'offline-test', 'api_key': 'fixture', 'model': 'test-model'}
        provider = {'provider': 'offline-provider', '_model_dict_cache': {'test-model': 'test-model'},
                    'preferences': {}}
        request = RequestModel(model='test-model', messages=[{'role': 'user', 'content': 'hi'}], stream=stream)
        with patch.object(process_module, 'get_payload', new=AsyncMock(return_value=('https://offline.invalid/v1/chat/completions', {}, {}))), \
             patch.object(process_module, 'get_engine', return_value=('openai', None, mode)), \
             patch.object(process_module, 'fetch_response', return_value=source), \
             patch.object(process_module, 'fetch_response_stream', return_value=source), \
             patch.object(process_module, 'error_handling_wrapper', new=AsyncMock(return_value=(source, 0.01))), \
             patch('core.handler._resolve_oauth_api_key', new=AsyncMock(return_value=None)), \
             patch('core.handler._fire_and_forget_channel_stats'), \
             patch('routes.stats.record_provider_activity'):
            response = await process_module.process_request(
                request, provider, BackgroundTasks(), app, lambda: info, AsyncMock(),
                dialect_id='openai-responses')
        return response, source

    async def route(self, response, *, stream=False, generic=False):
        if get_dialect('openai-responses') is None:
            register()
        dialect = get_dialect('openai-responses')
        endpoint = dialect.endpoints[0]
        # The production /v1/responses endpoint is custom, not generic.
        self.assertIsNotNone(endpoint.handler)
        handler = (_create_generic_handler if generic else _create_custom_handler_wrapper)(dialect.id, endpoint)
        body = json.dumps({'model': 'test-model', 'input': 'hi', 'stream': stream}).encode()

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        request = Request({'type': 'http', 'method': 'POST', 'path': '/v1/responses',
                           'headers': [(b'content-type', b'application/json')],
                           'query_string': b'', 'scheme': 'http', 'server': ('test', 80)}, receive)
        model_handler = SimpleNamespace(request_model=AsyncMock(return_value=response))
        with patch('routes.deps.get_model_handler', return_value=model_handler):
            result = await handler(request, BackgroundTasks(), api_index=0)
        self.assertEqual(model_handler.request_model.await_args.kwargs['dialect_id'], 'openai-responses')
        return result

    async def test_actual_non_stream_route_converts_once(self):
        response, source = await self.process()
        result = await self.route(response)
        self.assertIs(result, response)
        data = json.loads(''.join([part async for part in result.body_iterator]))
        self.assertEqual(data['status'], 'completed')
        self.assertEqual(data['output'][0]['content'][0]['text'], 'hello')
        self.assertEqual(data['usage']['total_tokens'], 5)
        self.assertTrue(source.closed)

    async def test_actual_stream_route_has_one_lifecycle(self):
        response, source = await self.process(stream=True)
        result = await self.route(response, stream=True)
        text = ''.join([part async for part in result.body_iterator])
        self.assertEqual(text.count('event: response.created\n'), 1)
        self.assertEqual(text.count('event: response.completed\n'), 1)
        self.assertNotIn('chat.completion', text)
        self.assertTrue(source.closed)

    async def test_generic_route_respects_explicit_rendered_marker(self):
        response, _ = await self.process()
        self.assertIs(await self.route(response, generic=True), response)
        await response.close()

    async def test_native_passthrough_remains_untouched(self):
        response = Response(b'{"object":"response","output":[]}',
                            media_type='application/json', headers={'x-zoaholic-passthrough': '1'})
        for generic in (False, True):
            self.assertIs(await self.route(response, generic=generic), response)

    async def test_close_before_first_iteration_releases_source(self):
        for stream in (False, True):
            response, source = await self.process(stream=stream)
            await response.close()
            self.assertTrue(source.closed)

    async def test_rendering_response_is_idempotent(self):
        data = await render_responses_response(canonical(), 'test-model')
        self.assertIs(await render_responses_response(data, 'test-model'), data)

    async def test_forced_non_stream_becomes_responses_stream(self):
        response, _ = await self.process(stream=True, mode='force_non_stream', values=[canonical()])
        text = ''.join([part async for part in response.body_iterator])
        self.assertIn('event: response.output_text.delta', text)
        self.assertEqual(text.count('event: response.completed\n'), 1)

    async def test_forced_stream_becomes_responses_json(self):
        values = [sse({'choices': [{'index': 0, 'delta': {'content': 'forced'}, 'finish_reason': 'stop'}]}),
                  'data: [DONE]\n\n']
        response, _ = await self.process(mode='force_stream', values=values)
        data = json.loads(''.join([part async for part in response.body_iterator]))
        self.assertEqual(data['output'][0]['content'][0]['text'], 'forced')

    async def test_upstream_error_preserves_status_and_code(self):
        error = {'message': 'rate limited', 'type': 'rate_limit_error', 'code': 'quota', 'status_code': 429}
        source = Source([UpstreamStreamError(error)])
        text = ''.join([part async for part in render_responses_iterator(source, 'test-model', stream=True)])
        events = [json.loads(frame.split('data: ', 1)[1]) for frame in text.strip().split('\n\n')]
        self.assertEqual(events[-1]['type'], 'response.failed')
        self.assertEqual(events[-1]['response']['error'], error)
        self.assertNotIn('event: response.completed', text)
        self.assertTrue(source.closed)

    async def test_fragmented_utf8_and_interleaved_tools(self):
        chunks = [
            {'choices': [{'delta': {'content': '你好', 'tool_calls': [
                {'index': 0, 'id': 'a', 'function': {'name': 'one', 'arguments': '{'}},
                {'index': 1, 'id': 'b', 'function': {'name': 'two', 'arguments': '{}'}}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '}'}}]}, 'finish_reason': 'tool_calls'}]},
            {'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}},
        ]
        raw = (''.join(sse(c) for c in chunks) + 'data: [DONE]\n\n').encode()
        text = ''.join([part async for part in render_responses_iterator(Source([bytes([b]) for b in raw]), 'test-model', stream=True)])
        events = [json.loads(frame.split('data: ', 1)[1]) for frame in text.strip().split('\n\n')]
        self.assertEqual([e['sequence_number'] for e in events], list(range(len(events))))
        final = events[-1]['response']
        self.assertEqual(final['usage']['total_tokens'], 3)
        calls = [item for item in final['output'] if item['type'] == 'function_call']
        self.assertEqual([(c['call_id'], c['arguments']) for c in calls], [('a', '{}'), ('b', '{}')])

    async def test_ws_forwards_rendered_events_without_second_conversion(self):
        from core.dialects.openai_responses_ws import _handle_sse_line
        response, source = await self.process(stream=True)
        websocket = SimpleNamespace(send_text=AsyncMock())
        async for chunk in response.body_iterator:
            for line in chunk.encode().splitlines():
                await _handle_sse_line(websocket, line)
        events = [json.loads(call.args[0]) for call in websocket.send_text.await_args_list]
        self.assertEqual(sum(e['type'] == 'response.created' for e in events), 1)
        self.assertEqual(sum(e['type'] == 'response.completed' for e in events), 1)
        self.assertEqual(events[-1]['response']['output'][0]['content'][0]['text'], 'hello')
        self.assertTrue(source.closed)

    async def test_truncated_stream_is_failed_not_completed(self):
        source = Source([sse({'choices': [{'delta': {'content': 'partial'}}]})])
        text = ''.join([part async for part in render_responses_iterator(source, 'test-model', stream=True)])
        self.assertIn('event: response.failed', text)
        self.assertNotIn('event: response.completed', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
