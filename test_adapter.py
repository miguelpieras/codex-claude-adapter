"""Bounded offline tests. Real Codex protocol; fake model endpoints and Claude CLI."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import adapter
import manage
import native
import paths

FAKE = '''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
if 'auth' in sys.argv:
 print(json.dumps({'loggedIn':True,'authMethod':'claude.ai','apiProvider':'firstParty','subscriptionType':'max'}));sys.exit()
request=json.loads(sys.stdin.read())
if Path('deny-auth').exists():
 print(json.dumps({'type':'result','is_error':True,'result':'OAuth access token has been revoked'}));sys.exit(1)
with Path('calls.jsonl').open('a') as f:f.write(json.dumps({'args':sys.argv,'envkeys':[k for k in os.environ if k.startswith(('ANTHROPIC_','OPENAI_','CLAUDE_'))],'subagent_model':os.environ.get('CLAUDE_CODE_SUBAGENT_MODEL'),'request':request})+'\\n')
if 'SLOW' in json.dumps(request):time.sleep(1)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','id':'tool_1','input':{'file_path':'fixture.txt'}}]}}),flush=True)
print(json.dumps({'type':'result','is_error':False,'result':'CLAUDE_FIXTURE history='+str('HISTORY_123' in json.dumps(request)), 'modelUsage':{sys.argv[sys.argv.index('--model')+1]:{}},'usage':{'input_tokens':10,'output_tokens':10},'permission_denials':[]}))
'''


def fake_cli(directory):
    path = directory / 'fake-claude'
    path.write_text(FAKE)
    path.chmod(0o755)
    return path


class DispatchTests(unittest.TestCase):
    def test_helpers_do_not_take_adapter_ownership(self):
        for args in (['app-server', '--listen', 'stdio://'],
                     ['app-server', 'proxy'], ['app-server', 'daemon', 'status'],
                     ['app-server', '-c', 'features.example=true', 'proxy'],
                     ['app-server', 'generate-ts'], ['app-server', '--help'],
                     ['sandbox', '--', 'node']):
            with self.subTest(args=args):
                self.assertFalse(adapter.wraps_server(args))
        for args in (['app-server'], ['app-server', '--stdio'],
                     ['-c', 'features.code_mode_host=true', 'app-server', '--analytics-default-enabled'],
                     ['app-server', '-c', 'model="proxy"', '--listen', 'stdio://']):
            with self.subTest(args=args):
                self.assertTrue(adapter.wraps_server(args))


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cli = fake_cli(self.root)
        self.runtime = native.NativeRuntime(self.root / 'state')
        self.thread = str(uuid.uuid4())
        self.runtime.bind(self.thread, self.root)
        self.patcher = patch.object(native, 'CLAUDE', self.cli)
        self.patcher.start()

    def tearDown(self):
        self.runtime.close()
        self.patcher.stop()
        self.temp.cleanup()

    def request(self, text='hello'):
        return {'model': native.MODEL, 'instructions': 'fixture',
                'input': [{'role': 'user', 'content': text}]}

    def run_turn(self, tid, request):
        return self.runtime.infer(tid, request, lambda text: None, lambda: False)

    def test_model_fail_closed(self):
        req = self.request()
        req['model'] = 'gpt-6-astra'
        with self.assertRaisesRegex(ValueError, 'No model fallback'):
            self.run_turn(self.thread, req)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_auth_revocation_no_fallback(self):
        (self.root / 'deny-auth').touch()
        with self.assertRaisesRegex(ValueError, 'No API fallback'):
            self.run_turn(self.thread, self.request())

    def test_subscription_environment(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fake', 'OPENAI_API_KEY': 'fake',
                                     'CLAUDE_CONFIG_DIR': '/wrong', 'CLAUDE_CODE_OAUTH_TOKEN': 'fake'}):
            self.run_turn(self.thread, self.request())
        call = json.loads((self.root / 'calls.jsonl').read_text())
        self.assertEqual(set(call['envkeys']), {'CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE'})
        self.assertIn('auto', call['args'])
        self.assertIn('none', call['args'])
        self.assertNotIn('--bare', call['args'])
        self.assertNotIn('--dangerously-skip-permissions', call['args'])

    def test_full_access_selects_bypass_mode_except_read_only(self):
        self.runtime.bind(self.thread, self.root, full_access=True)
        self.run_turn(self.thread, self.request('full'))
        side = str(uuid.uuid4())
        self.runtime.bind(side, self.root, readonly=True, full_access=True)
        self.run_turn(side, self.request('side'))
        modes = [v['args'][v['args'].index('--permission-mode') + 1]
                 for v in map(json.loads, (self.root / 'calls.jsonl').read_text().splitlines())]
        self.assertEqual(modes, ['bypassPermissions', 'auto'])

    def test_parallel_and_idempotent(self):
        other = str(uuid.uuid4())
        self.runtime.bind(other, self.root)
        started = time.monotonic()
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda tid: self.run_turn(tid, self.request('SLOW')), (self.thread, other)))
        self.assertLess(time.monotonic() - started, 1.9)
        self.run_turn(self.thread, self.request('SLOW'))
        self.assertEqual(len((self.root / 'calls.jsonl').read_text().splitlines()), 2)

    def test_continuation_uses_native_session(self):
        request = self.request('first')
        self.run_turn(self.thread, request)
        request['input'] += [{'role': 'assistant', 'content': 'answer'}, {'role': 'user', 'content': 'second'}]
        self.run_turn(self.thread, request)
        calls = [json.loads(v) for v in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertIn('--resume', calls[1]['args'])
        self.assertTrue(calls[1]['request']['continuation'])

    def test_effort_selection_and_model_transition(self):
        request = self.request('first')
        self.run_turn(self.thread, request)
        request['model'] = native.FABLE
        self.runtime.bind(self.thread, self.root, model=native.FABLE)
        for effort in ('xhigh', 'max', 'ultra'):
            request['reasoning'] = {'effort': effort}
            self.run_turn(self.thread, request)
        calls = [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()]
        self.assertEqual(len(calls), 4)
        for call, expected in zip(calls[1:], ('xhigh', 'max', 'ultracode')):
            self.assertEqual(call['args'][call['args'].index('--effort') + 1], expected)
            self.assertEqual(call['subagent_model'], native.FABLE)
            self.assertIn('--session-id', call['args'])
        self.run_turn(self.thread, request)
        self.assertEqual(len((self.root / 'calls.jsonl').read_text().splitlines()), 4)
        request['input'].append({'role': 'user', 'content': 'new message'})
        self.run_turn(self.thread, request)
        self.assertIn('--resume', json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])['args'])

    def test_unknown_effort_rejected_before_launch(self):
        for effort in ('extra', 'typo', 'ultracode'):
            req = {**self.request(), 'reasoning': {'effort': effort}}
            with self.assertRaisesRegex(ValueError, 'Unsupported Claude effort'):
                self.run_turn(self.thread, req)
        self.assertFalse((self.root / 'calls.jsonl').exists())

    def test_cancellation_blocks_replay(self):
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.run_turn, self.thread, self.request('SLOW'))
            deadline = time.monotonic() + 3
            while not self.runtime.running and time.monotonic() < deadline:
                time.sleep(.01)
            self.runtime.cancel(self.thread)
            with self.assertRaises(ValueError):
                future.result()
        with self.assertRaisesRegex(ValueError, 'automatic replay is blocked'):
            self.run_turn(self.thread, self.request('SLOW'))


class Client:
    def __init__(self, command, env):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, env=env)
        self.messages = queue.Queue()
        self.events = []
        self.serial = 0
        def reader():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
            self.messages.put({'eof': True})
        threading.Thread(target=reader, daemon=True).start()
        self.call('initialize', {'clientInfo': {'name': 'adapter_test', 'version': '1.0'},
                                 'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized'})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def next(self):
        item = self.messages.get(timeout=30)
        if item.get('eof'):
            raise RuntimeError('Wrapper exited early')
        return item

    def call(self, method, params):
        self.serial += 1
        ident = self.serial
        self.send({'id': ident, 'method': method, 'params': params})
        while True:
            item = self.next()
            if item.get('id') == ident:
                if 'error' in item:
                    raise RuntimeError(method + ': ' + str(item['error']))
                return item['result']
            self.events.append(item)

    def start_turn(self, tid, text, **kw):
        return self.call('turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text': text}], **kw})['turn']['id']

    def completed(self, turn):
        while True:
            for message in self.events:
                if message.get('method') == 'turn/completed' and message['params']['turn']['id'] == turn:
                    self.events.remove(message)
                    return message['params']['turn']
            self.events.append(self.next())

    def turn(self, tid, text, **kw):
        result = self.completed(self.start_turn(tid, text, **kw))
        if result['status'] != 'completed':
            raise RuntimeError(str(result))
        return result

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=30)
        self.process.stdout.close()


class Endpoint(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(data['model'])
        if data['model'] in native.MODELS:
            self.send_error(400, 'Opus leaked to native provider')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        stream = adapter.Stream(self, data['model'])
        stream.finish('NATIVE_FIXTURE', {})


class ProtocolTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CODEX_ADAPTER_INTEGRATION') == '1' and adapter.CODEX is not None,
                         'Set CODEX_ADAPTER_INTEGRATION=1 on macOS with Codex installed')
    def test_switch_fork_parallel_rollback(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                workspace = home / 'workspace'
                workspace.mkdir()
                runtime = home / 'claude-adapter'
                cli = fake_cli(home)
                source = Path.home() / '.codex/models_cache.json'
                (home / 'models_cache.json').write_text(source.read_text())
                config = ('model="gpt-6-astra"\nmodel_provider="fixture_native"\n'
                    'approval_policy="never"\nsandbox_mode="read-only"\nweb_search="disabled"\n'
                    '[analytics]\nenabled=false\n[model_providers.fixture_native]\nname="Fixture"\n'
                    'base_url="http://127.0.0.1:' + str(server.server_port) + '/v1"\n'
                    'wire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\n'
                    '[projects.' + json.dumps(str(workspace)) + ']\ntrust_level="trusted"\n')
                (home / 'config.toml').write_text(config)
                env = {k: v for k, v in os.environ.items() if not k.startswith(('OPENAI_', 'ANTHROPIC_', 'CLAUDE_', 'CODEX_'))}
                env['CODEX_HOME'] = str(home)
                env['CODEX_ADAPTER_APP'] = str(paths.APP.parents[2])
                code = ('import sys,asyncio;sys.path.insert(0,' + repr(str(adapter.ROOT)) + ');'
                    'import adapter,native;from pathlib import Path;'
                    'adapter.RUNTIME=Path(' + repr(str(runtime)) + ');'
                    'native.CLAUDE=Path(' + repr(str(cli)) + ');'
                    'asyncio.run(adapter.serve(["app-server","--stdio"]))')
                client = Client([sys.executable, '-c', code], env)
                try:
                    models = client.call('model/list', {})
                    self.assertTrue(set(native.MODELS) <= {m['model'] for m in models['data']})
                    for m in models['data']:
                        if m['model'] in native.MODELS:
                            self.assertEqual([e['reasoningEffort'] for e in m['supportedReasoningEfforts']], list(native.EFFORTS))
                    client.call('config/batchWrite', {'edits': [
                        {'keyPath': 'model', 'value': native.MODEL, 'mergeStrategy': 'replace'},
                        {'keyPath': 'model_reasoning_effort', 'value': 'high', 'mergeStrategy': 'replace'}]})
                    overlay = client.call('config/read', {})
                    self.assertEqual(overlay['config']['model'], native.MODEL)
                    self.assertEqual((home / 'config.toml').read_text(), config)
                    start = client.call('thread/start', {'cwd': str(workspace), 'model': 'gpt-6-astra'})
                    tid = start['thread']['id']
                    client.turn(tid, 'HISTORY_123')
                    owner = (runtime / 'owner.lock').read_text()
                    helper = Client([sys.executable, str(adapter.ROOT / 'adapter.py'),
                                     'app-server', '--listen', 'stdio://'], env)
                    try:
                        self.assertEqual(helper.call('thread/read', {'threadId': tid, 'includeTurns': False})['thread']['id'], tid)
                        self.assertFalse(set(native.MODELS) & {m['model'] for m in helper.call('model/list', {})['data']})
                        self.assertEqual((runtime / 'owner.lock').read_text(), owner)
                    finally:
                        helper.close()
                    client.call('thread/settings/update', {'threadId': tid, 'model': native.MODEL})
                    client.turn(tid, 'Continue as Opus.')
                    after = client.call('thread/read', {'threadId': tid, 'includeTurns': True})
                    self.assertEqual(after['thread']['modelProvider'], native.PROVIDER)
                    self.assertIn('history=True', json.dumps(after))
                    client.turn(tid, 'Continue as Fable at Max.', model=native.FABLE, effort='max')
                    fork = client.call('thread/fork', {'threadId': tid, 'ephemeral': True, 'excludeTurns': True})
                    self.assertEqual(fork['model'], native.FABLE)
                    self.assertEqual(fork['modelProvider'], native.PROVIDER)
                    other = client.call('thread/start', {'cwd': str(workspace), 'model': native.MODEL, 'sandbox': 'workspace-write'})
                    started = time.monotonic()
                    a = client.start_turn(tid, 'SLOW first')
                    b = client.start_turn(other['thread']['id'], 'SLOW second', effort='ultra')
                    self.assertEqual(client.completed(a)['status'], 'completed')
                    self.assertEqual(client.completed(b)['status'], 'completed')
                    self.assertLess(time.monotonic() - started, 2.5)
                    client.turn(fork['thread']['id'], 'Side chat', effort='xhigh')
                    calls = [json.loads(line) for line in (workspace / 'calls.jsonl').read_text().splitlines()]
                    self.assertTrue(any('ultracode' in call['args'] for call in calls))
                    self.assertTrue(any(native.FABLE in call['args'] and 'max' in call['args'] for call in calls))
                    for call in calls:
                        self.assertEqual(call['subagent_model'], call['args'][call['args'].index('--model') + 1])
                    with self.assertRaisesRegex(RuntimeError, 'Voice uses OpenAI'):
                        client.call('thread/realtime/start', {'threadId': tid})
                    client.turn(tid, 'Return to native', model='gpt-6-astra')
                    self.assertEqual(server.requests, ['gpt-6-astra', 'gpt-6-astra'])
                    self.assertEqual((home / 'config.toml').read_text(), config)
                finally:
                    client.close()
                self.assertEqual(manage.candidates(home), [])
                self.assertEqual(json.loads((runtime / 'restore.json').read_text()), {})
                self.assertEqual((home / 'config.toml').read_text(), config)
                # Simulate a crash, including an archived Opus task. Rollback must
                # use supported RPC and preserve both history and archive status.
                crashed = Client([sys.executable, '-c', code], env)
                saved = crashed.call('thread/start', {'cwd': str(workspace), 'model': native.MODEL})
                crashed.turn(saved['thread']['id'], 'HISTORY_123 crash recovery')
                archived = crashed.call('thread/start', {'cwd': str(workspace), 'model': native.FABLE})
                crashed.turn(archived['thread']['id'], 'HISTORY_123 archived')
                crashed.call('thread/archive', {'threadId': archived['thread']['id']})
                crashed.process.kill()
                crashed.process.wait(timeout=10)
                crashed.process.stdin.close()
                crashed.process.stdout.close()
                before = manage.candidates(home)
                self.assertEqual(len(before), 2)
                restored = asyncio.run(manage.migrate(home, runtime))
                self.assertEqual(set(restored), {saved['thread']['id'], archived['thread']['id']})
                self.assertEqual(manage.candidates(home), [])
                import sqlite3
                with sqlite3.connect(home / 'state_5.sqlite') as db:
                    self.assertEqual(db.execute('SELECT archived FROM threads WHERE id=?', (archived['thread']['id'],)).fetchone()[0], 1)
                self.assertEqual((home / 'config.toml').read_text(), config)
        finally:
            server.shutdown()
            server.server_close()


class PackagingTests(unittest.TestCase):
    def test_cleanup_does_not_claim_other_opus_providers(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            with sqlite3.connect(home / 'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT, model_provider TEXT, model TEXT, archived INTEGER)')
                db.executemany('INSERT INTO threads VALUES (?, ?, ?, ?)', [
                    ('ours', native.PROVIDER, native.MODEL, 0),
                    ('another-adapter', 'claude_subscription', native.MODEL, 0),
                    ('native', 'openai', 'gpt-6-astra', 0)])
            self.assertEqual([row['id'] for row in manage.candidates(home)], ['ours'])

    def test_app_discovery_verifies_product(self):
        import plistlib
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'Example.app'
            (root / 'Contents/MacOS').mkdir(parents=True)
            (root / 'Contents/Resources').mkdir()
            (root / 'Contents/MacOS/Example').touch()
            (root / 'Contents/Resources/codex').touch()
            info = root / 'Contents/Info.plist'
            with patch.dict(os.environ, {'CODEX_ADAPTER_APP': str(root)}):
                info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.chat', 'CFBundleExecutable': 'Example'}))
                self.assertEqual(paths.find_app(), (None, None))
                info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Example'}))
                self.assertEqual(paths.find_app()[1], (root / 'Contents/Resources/codex').resolve())
                nested = root / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
                nested.parent.mkdir(parents=True)
                nested.touch()
                self.assertEqual(paths.find_app(), ((root / 'Contents/MacOS/Example').resolve(), nested.resolve()))
                (root / 'Contents/Resources/codex').unlink()
                self.assertEqual(paths.find_app()[1], nested.resolve())
                nested.unlink()
                self.assertEqual(paths.find_app(), (None, None))

    def test_uninstall_preserves_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / 'generated'
            source = root / 'checkout'
            runtime.mkdir()
            source.mkdir()
            (source / 'user-changes.txt').write_text('keep')
            with patch.object(manage, 'RUNTIME', runtime), patch.object(manage, 'ROOT', source), \
                 patch.object(manage, 'app_running', return_value=False), \
                 patch.object(manage, 'migrate', new=AsyncMock(return_value=[])), patch('builtins.print'):
                manage.rollback(remove=True)
            self.assertFalse(runtime.exists())
            self.assertEqual((source / 'user-changes.txt').read_text(), 'keep')

    def test_uninstall_preserves_unrecognized_files(self):
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp)
            sentinel = runtime / 'user-notes.txt'
            sentinel.write_text('keep')
            with patch.object(manage, 'RUNTIME', runtime), patch.object(manage, 'app_running', return_value=False), \
                 patch.object(manage, 'migrate', new=AsyncMock(return_value=[])):
                with self.assertRaisesRegex(RuntimeError, 'Unexpected runtime files'):
                    manage.rollback(remove=True)
            self.assertEqual(sentinel.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main(verbosity=2)
