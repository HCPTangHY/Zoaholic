"""Offline Gemini model-list regressions; all HTTP uses MockTransport.

Run: python -B scripts/test_gemini_models.py
"""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DISABLE_DATABASE'] = 'true'

import httpx
from core.channels.gemini_channel import fetch_gemini_models
from core.plugins.interceptors import InterceptedClient

# Test a staged deployment candidate without replacing the live module.
if os.environ.get('ZOAHOLIC_GEMINI_TEST_SOURCE'):
    spec = importlib.util.spec_from_file_location(
        'core.channels._gemini_under_test', os.environ['ZOAHOLIC_GEMINI_TEST_SOURCE'])
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)
    fetch_gemini_models = candidate.fetch_gemini_models


class GeminiModelsTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler, provider=None, *, intercepted=False):
        provider = provider or {'base_url': 'https://gateway.invalid/v1', 'api': 'dummy-test-key'}
        original = copy.deepcopy(provider)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            if intercepted:
                async with InterceptedClient(client, 'gemini', provider) as wrapped:
                    result = await fetch_gemini_models(wrapped, provider)
            else:
                result = await fetch_gemini_models(client, provider)
        self.assertEqual(provider, original)
        return result

    async def test_official_gemini_auth_pagination_and_deduplication(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(str(request.url.copy_with(query=None)),
                             'https://generativelanguage.googleapis.com/v1beta/models')
            self.assertEqual(request.headers['x-goog-api-key'], 'dummy-test-key')
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('key', request.url.params)
            self.assertEqual(request.url.params['pageSize'], '1000')
            if len(calls) == 1:
                return httpx.Response(200, json={'models': [{'name': 'models/gemini-one'},
                    {'name': 'models/gemini-one'}, None, {'name': 1}], 'nextPageToken': 'page2'})
            self.assertEqual(request.url.params['pageToken'], 'page2')
            return httpx.Response(200, json={'models': [{'name': 'models/gemini-two'}]})
        result = await self.fetch(handler, {'api': 'dummy-test-key'})
        self.assertEqual(result, ['gemini-one', 'gemini-two'])
        self.assertEqual(len(calls), 2)

    async def test_compatibility_gateway_auth_fallback_401_and_403(self):
        for status in (401, 403):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    self.assertEqual(request.method, 'GET')
                    self.assertEqual(request.headers['x-goog-api-key'], 'dummy-test-key')
                    self.assertNotIn('key', request.url.params)
                    if not request.headers.get('authorization'):
                        return httpx.Response(status, json={'error': {'message': 'Invalid or missing API Key'}})
                    self.assertEqual(request.headers['authorization'], 'Bearer dummy-test-key')
                    self.assertEqual(request.url, calls[0].url)
                    return httpx.Response(200, json={'data': [{'id': 'gemini-one'}, {'id': 'gemini-two'}]})
                self.assertEqual(await self.fetch(handler), ['gemini-one', 'gemini-two'])
                self.assertEqual(len(calls), 2)

    async def test_openai_shape_on_first_success_preserves_ids(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'data': [{'id': ' models/custom-id '},
                {'id': 'models/custom-id'}, {'id': 'gemini-two'}, {}, {'id': 3}, None],
                'nextPageToken': 'not-a-gemini-page'})
        self.assertEqual(await self.fetch(handler), ['models/custom-id', 'gemini-two'])
        self.assertEqual(len(calls), 1)

    async def test_failed_fallback_is_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(403, json={'error': 'bad credentials'})
        with self.assertRaises(httpx.HTTPStatusError) as caught:
            await self.fetch(handler)
        self.assertEqual(caught.exception.response.status_code, 403)
        self.assertEqual(len(calls), 2)

    async def test_no_fallback_for_official_google(self):
        for host in ('generativelanguage.googleapis.com', 'us-central1-aiplatform.googleapis.com'):
            with self.subTest(host=host):
                calls = []
                def handler(request):
                    calls.append(request)
                    self.assertNotIn('authorization', request.headers)
                    return httpx.Response(403, json={'error': 'invalid Google key'})
                with self.assertRaises(httpx.HTTPStatusError):
                    await self.fetch(handler, {'base_url': f'https://{host}/v1beta', 'api': 'dummy-test-key'})
                self.assertEqual(len(calls), 1)

    async def test_non_auth_errors_do_not_retry(self):
        for status in (404, 429, 500, 524):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, json={'error': 'upstream error'})
                with self.assertRaises(httpx.HTTPStatusError):
                    await self.fetch(handler)
                self.assertEqual(len(calls), 1)

    async def test_custom_authorization_is_preserved_by_real_wrapper(self):
        def handler(request):
            self.assertEqual(request.headers['authorization'], 'Bearer custom-test-token')
            self.assertEqual(request.headers['x-test-parent'], 'inherited')
            return httpx.Response(200, json={'data': [{'id': 'gemini-custom'}]})
        provider = {'base_url': 'https://gateway.invalid/v1', 'api': 'dummy-test-key',
                    'preferences': {'headers': {'authorization': 'Bearer custom-test-token',
                                                'X-Test-Parent': 'inherited'}}}
        self.assertEqual(await self.fetch(handler, provider, intercepted=True), ['gemini-custom'])

    async def test_inherited_custom_headers_survive_auth_fallback(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers['x-test-parent'], 'inherited')
            self.assertEqual(request.headers['x-goog-api-key'], 'dummy-test-key')
            if len(calls) == 1:
                self.assertNotIn('authorization', request.headers)
                return httpx.Response(403, json={'error': 'Bearer required for model list'})
            self.assertEqual(request.headers['authorization'], 'Bearer dummy-test-key')
            self.assertEqual(request.url, calls[0].url)
            return httpx.Response(200, json={'data': [{'id': 'gemini-inherited'}]})
        provider = {'base_url': 'https://gateway.invalid/v1', 'api': 'dummy-test-key',
                    'preferences': {'headers': {'X-Test-Parent': 'inherited'}}}
        self.assertEqual(await self.fetch(handler, provider, intercepted=True), ['gemini-inherited'])
        self.assertEqual(len(calls), 2)

    async def test_explicit_authorization_failure_is_not_overridden(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers['authorization'], 'Bearer custom-test-token')
            return httpx.Response(403, json={'error': 'custom token denied'})
        provider = {'base_url': 'https://gateway.invalid/v1', 'api': 'dummy-test-key',
                    'preferences': {'headers': {'Authorization': 'Bearer custom-test-token'}}}
        with self.assertRaises(httpx.HTTPStatusError):
            await self.fetch(handler, provider, intercepted=True)
        self.assertEqual(len(calls), 1)

    async def test_exact_url_marker_is_preserved_during_fallback(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.url.path, '/custom/model-list')
            if len(calls) == 1:
                return httpx.Response(401)
            return httpx.Response(200, json={'data': [{'id': 'model'}]})
        result = await self.fetch(handler, {'base_url': 'https://gateway.invalid/custom/model-list#',
                                           'api': 'dummy-test-key'})
        self.assertEqual(result, ['model'])
        self.assertEqual(len(calls), 2)

    async def test_fallback_auth_is_reused_for_gemini_pages(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(403)
            self.assertEqual(request.headers['authorization'], 'Bearer dummy-test-key')
            if len(calls) == 2:
                return httpx.Response(200, json={'models': [{'name': 'models/one'}], 'next_page_token': 'p2'})
            self.assertEqual(request.url.params['pageToken'], 'p2')
            return httpx.Response(200, json={'models': [{'name': 'models/two'}]})
        self.assertEqual(await self.fetch(handler), ['one', 'two'])
        self.assertEqual(len(calls), 3)

    async def test_native_format_takes_precedence(self):
        def handler(request):
            return httpx.Response(200, json={'models': [{'name': 'models/native'}], 'data': [{'id': 'other'}]})
        self.assertEqual(await self.fetch(handler), ['native'])

    async def test_non_object_response_is_empty(self):
        self.assertEqual(await self.fetch(lambda request: httpx.Response(200, json=[])), [])

    async def test_model_count_is_bounded_for_both_formats(self):
        for key, field in (('models', 'name'), ('data', 'id')):
            with self.subTest(key=key):
                def handler(request):
                    return httpx.Response(200, json={key: [{field: f'model-{i}'} for i in range(5001)]})
                self.assertEqual(len(await self.fetch(handler)), 5000)

    async def test_page_count_is_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'models': [{'name': f'model-{len(calls)}'}],
                                             'nextPageToken': 'loop'})
        self.assertEqual(len(await self.fetch(handler)), 20)
        self.assertEqual(len(calls), 20)


if __name__ == '__main__':
    unittest.main(verbosity=2)
