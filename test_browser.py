"""Offline transport, task attribution and permission-boundary tests."""
import asyncio
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from adapter import Handler
from browser import BrowserBridge
import test_adapter


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.worker = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.worker.start()
        self.core = SimpleNamespace(call=AsyncMock())
        self.adapter = SimpleNamespace(core=self.core, active={'one': 'turn1', 'two': 'turn2'},
            native=SimpleNamespace(bindings={'one': {'readonly': False}, 'two': {'readonly': False}}))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.bridge = self.server.browser = BrowserBridge(self.adapter, self.loop, self.server.server_port)
        self.http_worker = threading.Thread(target=self.server.serve_forever)
        self.http_worker.start()
        self.core.call.return_value = {'data': [{'name': 'cua_repl',
            'pluginId': 'unified-computer-use@openai-bundled', 'tools': {
                'js': {'name': 'js', 'description': 'First call: getState.',
                       'inputSchema': {'type': 'object'}},
                'turn_ended': {'name': 'turn_ended', 'inputSchema': {'type': 'object'}}}}]}
        self.metadata = {'session_id': 'one', 'thread_id': 'one', 'turn_id': 'turn1',
                         'model': 'claude-opus-5-5', 'auto_review_enabled': False}
        self.token, self.config = self.bridge.open('one', self.metadata)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.http_worker.join()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.worker.join()
        self.loop.close()

    def request(self, method, params=None, token=None, headers=None):
        req = Request(self.bridge.url, data=json.dumps({'jsonrpc': '2.0', 'id': 1,
            'method': method, 'params': params or {}}).encode(), headers={
                'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (token or self.token),
                **(headers or {})})
        with urlopen(req, timeout=3) as response:
            return json.load(response)['result']

    def test_catalog_authentication_and_origin(self):
        self.assertEqual(self.request('initialize')['capabilities'], {'tools': {}})
        tools = self.request('tools/list')['tools']
        self.assertEqual([v['name'] for v in tools], ['js'])
        self.assertFalse(tools[0]['annotations']['readOnlyHint'])
        for kw in ({'token': 'invalid'}, {'headers': {'Origin': 'https://example.com'}}):
            with self.assertRaises(HTTPError) as error:
                self.request('tools/list', **kw)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_images_errors_and_real_thread_attribution(self):
        image = {'type': 'image', 'mimeType': 'image/png', 'data': 'fixture-base64'}
        expected = {'content': [image, {'type': 'text', 'text': 'Browser state'}], 'isError': False}
        self.core.call.return_value = expected
        self.assertEqual(self.request('tools/call', {'name': 'js', 'arguments': {'code': 'await cua.getState();'}}), expected)
        method, params = self.core.call.call_args.args
        self.assertEqual(method, 'mcpServer/tool/call')
        self.assertEqual(params['threadId'], 'one')
        self.assertEqual(params['_meta']['x-codex-turn-metadata'], self.metadata)
        self.assertEqual(params['server'], 'cua_repl')
        self.core.call.return_value = {'content': [{'type': 'text', 'text': 'Policy denied'}], 'isError': True}
        self.assertTrue(self.request('tools/call', {'name': 'js', 'arguments': {'code': 'await cua.getState();'}})['isError'])

    def test_closed_readonly_cross_turn_and_unexposed_tools(self):
        self.core.call.reset_mock()
        for params in ({'name': 'shell', 'arguments': {}},
                       {'name': 'js', 'arguments': {'code': '1', 'timeout_ms': 60001}},
                       {'name': 'js', 'arguments': {'code': '1', 'threadId': 'two'}}):
            self.assertTrue(self.request('tools/call', params)['isError'])
        self.adapter.native.bindings['one']['readonly'] = True
        self.assertIsNone(self.bridge.open('one', self.metadata))
        self.assertTrue(self.request('tools/call', {'name': 'js', 'arguments': {'code': '1'}})['isError'])
        self.adapter.native.bindings['one']['readonly'] = False
        self.adapter.active['one'] = 'newturn'
        self.assertTrue(self.request('tools/call', {'name': 'js', 'arguments': {'code': '1'}})['isError'])
        self.core.call.assert_not_called()
        self.bridge.close(self.token)
        with self.assertRaises(HTTPError) as error:
            self.request('tools/list')
        error.exception.close()

    def test_parallel_tasks_keep_separate_capabilities(self):
        token2, _ = self.bridge.open('two', {'session_id': 'two', 'turn_id': 'turn2'})
        self.assertNotEqual(self.token, token2)
        self.bridge.cancel('one')
        with self.assertRaises(HTTPError) as error:
            self.request('tools/list')
        error.exception.close()
        self.assertEqual(len(self.request('tools/list', token=token2)['tools']), 1)

    def test_missing_or_mismatched_attribution_never_uses_browser(self):
        self.assertIsNone(self.bridge.open('one', None))
        self.assertIsNone(self.bridge.open('one', {**self.metadata, 'auto_review_enabled': True}))
        self.assertIsNone(self.bridge.open('one', {**self.metadata, 'node_repl_auto_review_required': True}))
        for values in ({'turn_id': 'other'}, {'thread_id': 'two'}, {'model': 'gpt-6-astra'}):
            with self.assertRaises(ValueError):
                self.bridge.open('one', {**self.metadata, **values})


class NativeBrowserTests(unittest.TestCase):
    # Reuse the fake CLI setup without inheriting its test cases.
    setUp = test_adapter.NativeTests.setUp
    tearDown = test_adapter.NativeTests.tearDown
    request = test_adapter.NativeTests.request
    run_turn = test_adapter.NativeTests.run_turn

    def test_browser_config_lifetime_and_readonly(self):
        browser = Mock()
        browser.open.return_value = ('private-token', {'mcpServers': {'codex_browser': {}}})
        self.runtime.browser = browser
        self.run_turn(self.thread, self.request())
        call = json.loads((self.root / 'calls.jsonl').read_text())
        self.assertIn('--restricted', call['args'])
        self.assertIn('--strict-mcp-config', call['args'])
        self.assertNotIn('--safe-mode', call['args'])
        self.assertNotIn('--allowedTools', call['args'])
        browser.close.assert_called_once_with('private-token')
        browser.reset_mock()
        self.runtime.bind(self.thread, self.root, readonly=True)
        self.run_turn(self.thread, self.request('read-only'))
        browser.open.assert_not_called()
        call = json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])
        self.assertIn('--safe-mode', call['args'])
        self.assertIn('Read,Glob,Grep', call['args'])


if __name__ == '__main__':
    unittest.main()
