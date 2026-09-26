import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import os
import hashlib
import asyncio
from unittest.mock import Mock, patch

import standalone as service
import native
import claude_mode
from paths import CODEX
from test_adapter import Client, fake_cli


class FailClosedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.server = service.service(Path(self.directory.name) / 'sessions', home=Path(self.directory.name))
        self.thread = str(uuid.uuid4())
        self.server.native.bind(self.thread, self.directory.name)
        self.server.native.infer = Mock(side_effect=AssertionError('No inference expected'))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.metadata = {'thread_id': self.thread, 'model': service.native.MODEL,
                         'turn_id': str(uuid.uuid4()), 'auto_review_enabled': False,
                         'node_repl_auto_review_required': False, 'sandbox_mode': 'workspace-write',
                         'workspaces': {self.directory.name: {}}, 'reasoning_effort': 'xhigh'}

    def tearDown(self):
        self.server.native.close(); self.server.shutdown(); self.server.server_close()
        self.directory.cleanup()

    def request(self, *, model=service.native.MODEL, token=True, metadata=None, tid=None):
        data = {'model': model, 'input': [], 'client_metadata': {
            'x-codex-turn-metadata': json.dumps(metadata if metadata is not None else self.metadata)}}
        headers = {'Content-Type': 'application/json', 'thread-id': tid or self.thread,
                   'X-Local-Claude-Token': self.server.token if token else 'wrong'}
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/v1/responses',
            data=json.dumps(data).encode(), headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=5)
        self.server.native.infer.assert_not_called()
        code = caught.exception.code
        caught.exception.close()
        return code

    def test_unknown_model_never_calls_inference(self):
        self.assertEqual(self.request(model='gpt-6-astra'), 400)

    def test_wrong_task_never_calls_inference(self):
        self.assertEqual(self.request(tid=str(uuid.uuid4())), 400)

    def test_bad_token_never_calls_inference(self):
        self.assertEqual(self.request(token=False), 403)

    def test_required_host_model_review_is_not_bypassed(self):
        for key in ('auto_review_enabled', 'node_repl_auto_review_required'):
            self.assertEqual(self.request(metadata={**self.metadata, key: True}), 400)
        self.assertEqual(self.request(metadata={}), 400)

    def test_actual_attribution_must_match(self):
        self.assertEqual(self.request(metadata={**self.metadata, 'model': 'gpt-6-astra'}), 400)


class ConversionTests(unittest.TestCase):
    def test_native_inline_images_are_framed_and_remote_urls_rejected(self):
        message = native.inline_image_message({'input': [{'type': 'input_image',
            'image_url': 'data:image/png;base64,YQ=='}]})
        content = message['message']['content']
        self.assertEqual(content[-1]['source']['data'], 'YQ==')
        self.assertEqual(json.loads(content[0]['text'])['input'][0]['number'], 1)
        with self.assertRaises(ValueError):
            native.inline_image_message({'type': 'input_image', 'image_url': 'https://example.com/x.png'})

    def test_long_socket_path_rejected_before_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ('x' * 105)
            with patch.object(claude_mode, 'require_codex'), self.assertRaisesRegex(RuntimeError, 'too long'):
                claude_mode.prepare(directory)
            self.assertFalse(directory.exists())

    def test_namespaced_tools_preserve_schema_and_exclude_host_agents(self):
        schema = {'type': 'object', 'properties': {'code': {'type': 'string'}}, 'required': ['code']}
        tools = service.descriptors([
            {'type': 'namespace', 'name': 'mcp__cua_repl', 'tools': [{'type': 'function', 'name': 'js', 'parameters': schema}]},
            {'type': 'namespace', 'name': 'multi_agent_v1', 'tools': [{'type': 'function', 'name': 'spawn_agent', 'parameters': {}}]}])
        self.assertEqual(list(tools), ['mcp__cua_repl__js'])
        self.assertEqual(tools['mcp__cua_repl__js']['parameters'], schema)
        self.assertEqual(tools['mcp__cua_repl__js']['namespace'], 'mcp__cua_repl')

    def test_images_remain_mcp_images_and_remote_urls_are_not_fetched(self):
        result = service.mcp_result([{'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='}])
        self.assertEqual(result['content'][0], {'type': 'image', 'mimeType': 'image/png', 'data': 'YQ=='})
        with self.assertRaises(ValueError):
            service.mcp_result([{'type': 'input_image', 'image_url': 'https://example.com/image.png'}])


class CoreTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CODEX_ADAPTER_INTEGRATION') == '1' and CODEX,
                         'Requires the official bundled Codex server')
    def test_isolated_profile_models_efforts_parallel_and_sidechat(self):
        normal = Path.home() / '.codex/config.toml'
        before = hashlib.sha256(normal.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(dir='/tmp') as temporary:
            directory = Path(temporary).resolve()
            home = claude_mode.prepare(directory)
            server = service.service(directory / 'sessions', home=home)
            claude_mode.configure(home, {'port': server.server_port, 'token': server.token})
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cli = fake_cli(directory)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'OPENAI_', 'ANTHROPIC_'))}
            env['CODEX_HOME'] = str(home)
            with patch.object(native, 'CLAUDE', cli):
                client = Client([str(CODEX), 'app-server', '--stdio'], env)
                try:
                    models = client.call('model/list', {})
                    self.assertEqual({v['model'] for v in models['data']}, set(native.MODELS))
                    task = client.call('thread/start', {'cwd': str(directory)})
                    self.assertEqual(task['modelProvider'], service.PROVIDER)
                    tid = task['thread']['id']
                    client.turn(tid, 'Initial Opus message', effort='xhigh')
                    client.turn(tid, 'Fable message', model=native.FABLE, effort='max')
                    child = client.call('thread/fork', {'threadId': tid, 'ephemeral': True,
                        'excludeTurns': True, 'sandbox': 'read-only', 'model': native.FABLE})
                    self.assertEqual(child['modelProvider'], service.PROVIDER)
                    self.assertEqual(child['model'], native.FABLE)
                    client.turn(child['thread']['id'], 'Read-only side chat', effort='high')
                    other = client.call('thread/start', {'cwd': str(directory), 'model': native.MODEL})
                    a = client.start_turn(tid, 'SLOW Fable parallel', effort='ultra')
                    b = client.start_turn(other['thread']['id'], 'SLOW Opus parallel', effort='xhigh')
                    self.assertEqual(client.completed(a)['status'], 'completed')
                    self.assertEqual(client.completed(b)['status'], 'completed')
                    calls = [json.loads(v) for v in (directory / 'calls.jsonl').read_text().splitlines()]
                    efforts = [v['args'][v['args'].index('--effort') + 1] for v in calls]
                    self.assertIn('ultracode', efforts)
                    self.assertIn('max', efforts)
                    self.assertTrue(any(v['args'][v['args'].index('--tools') + 1] == 'Read,Glob,Grep' for v in calls))
                    self.assertTrue(all(v['subagent_model'] == v['args'][v['args'].index('--model') + 1] for v in calls))
                    self.assertEqual(set(v['model'] for v in server.requests), set(native.MODELS))
                finally:
                    server.native.close(); client.close()
                    server.shutdown(); server.server_close()
        self.assertEqual(hashlib.sha256(normal.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()

class RelayTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir='/tmp')
        self.home = Path(self.directory.name)
        self.server = service.service(self.home / 'sessions', home=self.home)
        self.tid = str(uuid.uuid4())
        self.metadata = {'thread_id': self.tid, 'turn_id': str(uuid.uuid4()),
            'model': native.MODEL, 'reasoning_effort': 'low', 'auto_review_enabled': False,
            'node_repl_auto_review_required': False, 'sandbox_mode': 'workspace-write',
            'workspaces': {str(self.home): {}}}
        self.request = {'model': native.MODEL, 'input': [], 'tools': [
            {'type': 'namespace', 'name': 'mcp__cua_repl', 'tools': [
                {'type': 'function', 'name': 'js', 'parameters': {'type': 'object'}}]}],
            'client_metadata': {'x-codex-turn-metadata': json.dumps(self.metadata)}}
        self.native_calls = 0
        self.result = None
        def infer(tid, request, emit, cancelled, **kwargs):
            self.native_calls += 1
            token, _ = self.server.browser.open(tid, self.metadata, model=native.MODEL)
            try:
                self.result = asyncio.run(self.server.browser.call(token, 'mcp__cua_repl__js', {'code': 'fixture'}))
                native.atomic_json(self.server.native.directory / (tid + '.json'),
                    {'status': 'completed', 'result': 'host result received', 'usage': {}})
                return 'host result received', {}
            finally:
                self.server.browser.close(token)
        self.server.native.infer = infer
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.native.close()
        for turn in self.server.turns.values():
            turn.stopped.set()
            turn.done.wait(2)
        self.server.shutdown(); self.server.server_close()
        self.directory.cleanup()

    def post(self, request):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/v1/responses',
            data=json.dumps(request).encode(), headers={'Content-Type': 'application/json',
                'thread-id': self.tid, 'X-Local-Claude-Token': self.server.token})
        with urllib.request.urlopen(req, timeout=5) as response:
            events = [json.loads(line[6:]) for line in response.read().decode().splitlines() if line.startswith('data: ')]
        return next(v['response'] for v in reversed(events) if v.get('type') == 'response.completed')

    def test_host_image_continuation_and_replay_do_not_repeat_native_work(self):
        first = self.post(self.request)
        call = next(v for v in first['output'] if v['type'] == 'function_call')
        self.assertEqual(call['namespace'], 'mcp__cua_repl')
        continuation = {**self.request, 'input': [call, {'type': 'function_call_output',
            'call_id': call['call_id'], 'output': [{'type': 'input_image', 'image_url': 'data:image/png;base64,YQ=='}]}]}
        self.post(continuation)
        self.assertEqual(self.result['content'][0]['type'], 'image')
        self.post(continuation)
        self.assertEqual(self.native_calls, 1)
        state = json.loads((self.server.native.directory / (self.tid + '.json')).read_text())
        self.assertEqual(state['input_count'], 2)
        self.assertEqual(state['input_digest'], native.digest(continuation['input']))

    def test_wrong_tool_result_cancels_instead_of_replaying(self):
        self.post(self.request)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post({**self.request, 'input': [{'type': 'function_call_output', 'call_id': 'wrong', 'output': 'x'}]})
        caught.exception.close()
        turn = self.server.turns[self.tid]
        self.assertTrue(turn.stopped.is_set())
        self.assertTrue(turn.done.wait(2))
        self.assertEqual(self.native_calls, 1)

    def test_duplicate_http_request_does_not_cancel_existing_turn(self):
        self.post(self.request)
        turn = self.server.turns[self.tid]
        turn.http_lock.acquire()
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.post(self.request)
            caught.exception.close()
            self.assertFalse(turn.stopped.is_set())
        finally:
            turn.http_lock.release()

    def test_message_to_openai_or_unverified_target_is_rejected(self):
        turn = service.Turn(self.tid, {**self.request, 'tools': [
            {'type': 'namespace', 'name': 'mcp__codex_app', 'tools': [{'type': 'function',
                'name': 'send_message_to_thread', 'parameters': {'type': 'object'}}]}]}, self.metadata)
        self.server.turns[self.tid] = turn
        token, _ = self.server.browser.open(self.tid, self.metadata, model=native.MODEL)
        try:
            with self.assertRaisesRegex(ValueError, 'same Claude model/provider'):
                asyncio.run(self.server.browser.call(token, 'mcp__codex_app__send_message_to_thread',
                    {'threadId': str(uuid.uuid4()), 'prompt': 'fixture'}))
            with self.assertRaisesRegex(ValueError, 'preserve'):
                asyncio.run(self.server.browser.call(token, 'mcp__codex_app__send_message_to_thread',
                    {'threadId': self.tid, 'model': 'gpt-6-astra', 'prompt': 'fixture'}))
        finally:
            turn.done.set()
            self.server.browser.close(token)

class RemovalTests(unittest.TestCase):
    def test_missing_updated_app_fails_before_reusing_service(self):
        with patch.object(claude_mode, 'require_codex', side_effect=RuntimeError('bundled CLI missing')), \
             patch.object(claude_mode, 'start') as start, patch.object(claude_mode.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'bundled CLI missing'):
                claude_mode.launch()
            start.assert_not_called()
            launch.assert_not_called()

    def test_unrelated_directory_and_running_window_are_preserved(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            directory = Path(tmp) / 'owned'
            directory.mkdir()
            sentinel = directory / 'keep.txt'
            sentinel.write_text('history')
            with self.assertRaisesRegex(RuntimeError, 'not marked'):
                claude_mode.remove(directory, delete_history=True)
            self.assertTrue(sentinel.exists())
            native.atomic_json(directory / 'owner.json', {'provider': service.PROVIDER, 'version': 1})
            with patch.object(claude_mode, 'app_running', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'Close'):
                    claude_mode.remove(directory, delete_history=True)
            self.assertTrue(sentinel.exists())

    def test_removal_retains_history_unless_explicitly_requested(self):
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            directory = Path(tmp) / 'owned'
            directory.mkdir()
            native.atomic_json(directory / 'owner.json', {'provider': service.PROVIDER, 'version': 1})
            sentinel = directory / 'keep.txt'
            sentinel.write_text('history')
            with patch.object(claude_mode, 'app_running', return_value=False), patch.object(claude_mode, 'control'), patch('dock.remove'):
                claude_mode.remove(directory)
                self.assertTrue(sentinel.exists())
                claude_mode.remove(directory, delete_history=True)
                self.assertFalse(directory.exists())

    def test_standard_launch_clears_adapter_environment(self):
        with patch.object(claude_mode, 'require_codex'), patch('manage.candidates', return_value=[]), patch.object(claude_mode, 'control'), patch.object(claude_mode.subprocess, 'Popen') as launch:
            with patch.dict(os.environ, {'CODEX_HOME': '/tmp/claude', 'CODEX_CLI_PATH': '/tmp/wrapper', 'CODEX_ELECTRON_USER_DATA_PATH': '/tmp/profile'}):
                claude_mode.standard()
            environment = launch.call_args.kwargs['env']
            self.assertFalse(any(k.startswith('CODEX_') for k in environment))
            self.assertEqual(launch.call_args.args[0], [str(claude_mode.APP)])
