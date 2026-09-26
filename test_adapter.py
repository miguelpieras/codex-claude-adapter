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
from unittest.mock import AsyncMock, Mock, patch

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
model=sys.argv[sys.argv.index('--model')+1]
print(json.dumps({'type':'system','subtype':'init','model':model,'apiKeySource':'ANTHROPIC_API_KEY' if 'BADKEY' in json.dumps(request) else 'none'}),flush=True)
final='CLAUDE_FIXTURE history='+str('HISTORY_123' in json.dumps(request))
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','id':'tool_1','input':{'file_path':'fixture.txt'}}]}}),flush=True)
if 'STREAM' in json.dumps(request):
 for e in [{'type':'stream_event','event':{'type':'message_start','message':{'usage':{'input_tokens':5,'cache_read_input_tokens':(990000 if 'HUGE' in json.dumps(request) else 40000),'cache_creation_input_tokens':100}}},'parent_tool_use_id':None},
  {'type':'stream_event','event':{'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':7}},'parent_tool_use_id':None},
  {'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'Plan: '}},'parent_tool_use_id':None},
  {'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'read files'}},'parent_tool_use_id':None},
  {'type':'assistant','message':{'content':[{'type':'thinking','thinking':'Plan: read files'}]},'parent_tool_use_id':None},
  {'type':'assistant','message':{'content':[{'type':'text','text':'Looking around.'}]},'parent_tool_use_id':None},
  {'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash','id':'t2','input':{'command':'npm test'}}]},'parent_tool_use_id':None},
  {'type':'assistant','message':{'content':[{'type':'tool_use','name':'Grep','id':'t3','input':{'pattern':'TODO'}}]},'parent_tool_use_id':'t9','task_description':'Scan'},
  {'type':'assistant','message':{'content':[{'type':'thinking','thinking':'subagent thought'}]},'parent_tool_use_id':'t9'},
  {'type':'system','subtype':'permission_denied','tool_name':'Write'},
  {'type':'assistant','message':{'content':[{'type':'text','text':final}]},'parent_tool_use_id':None}]:
  print(json.dumps(e),flush=True)
if 'AGENTS' in json.dumps(request):
 for e in [{'type':'assistant','message':{'content':[{'type':'tool_use','name':'Agent','id':'ag1','input':{'description':'Audit billing','prompt':'x'}}]},'parent_tool_use_id':None},
  {'type':'assistant','message':{'content':[{'type':'tool_use','name':'Read','id':'r1','input':{'file_path':'/w/src/refunds.ts'}}]},'parent_tool_use_id':'ag1','task_description':'Audit billing'},
  {'type':'assistant','message':{'content':[{'type':'tool_use','name':'mcp__codex_browser__exec_command','id':'x1','input':{'cmd':'wc -l a.py'}}]},'parent_tool_use_id':'ag1','task_description':'Audit billing'},
  {'type':'assistant','message':{'content':[{'type':'text','text':'Checking the page.'}]},'parent_tool_use_id':None},
  {'type':'assistant','message':{'content':[{'type':'tool_use','name':'mcp__codex_browser__js','id':'b1','input':{'code':'1'}}]},'parent_tool_use_id':None},
  {'type':'system','subtype':'task_notification','tool_use_id':'ag1','status':'completed'},
  {'type':'assistant','message':{'content':[{'type':'text','text':final}]},'parent_tool_use_id':None}]:
  print(json.dumps(e),flush=True)
print(json.dumps({'type':'result','is_error':False,'result':final, 'modelUsage':{sys.argv[sys.argv.index('--model')+1]:{}},'usage':{'input_tokens':10,'output_tokens':10},'permission_denials':[]}))
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


class StreamTests(unittest.TestCase):
    def events(self, run):
        handler = type('Handler', (), {})()
        handler.wfile = __import__('io').BytesIO()
        stream = adapter.Stream(handler, native.MODEL)
        run(stream)
        return [json.loads(line[6:]) for line in handler.wfile.getvalue().decode().splitlines() if line.startswith('data: ')]

    def test_progress_items_are_well_formed_and_closed(self):
        def run(stream):
            stream.emit('Plan', 'thinking'); stream.emit(' more', 'thinking'); stream.emit('', 'thinking_done')
            stream.emit('Running ls', 'status'); stream.emit('Reading a', 'status')
            stream.emit('Ran `ls`, read `a`')
            stream.emit('Scan: Searching for x', 'status'); stream.emit('next thought', 'thinking')
            stream.emit('', 'thinking_done')
            stream.emit('', 'thinking_done')  # nothing open: no empty item
            stream.finish('answer', {})
        events = self.events(run)
        added = [e['item'] for e in events if e['type'] == 'response.output_item.added']
        done = [e['item'] for e in events if e['type'] == 'response.output_item.done']
        self.assertEqual([i['id'] for i in added], [i['id'] for i in done])
        self.assertEqual([i['type'] for i in done], ['reasoning', 'reasoning', 'message', 'reasoning', 'message'])
        self.assertEqual([i['summary'][0]['text'] for i in done if i['type'] == 'reasoning'],
                         ['Plan more', 'Running ls\n\nReading a', 'Scan: Searching for x\n\nnext thought'])
        self.assertEqual([(i['phase'], i['content'][0]['text']) for i in done if i['type'] == 'message'],
                         [('commentary', 'Ran `ls`, read `a`'), ('final_answer', 'answer')])
        completed = next(e for e in events if e['type'] == 'response.completed')['response']
        self.assertEqual(completed['output'], done)

    def test_heartbeat_is_a_real_event_not_a_comment(self):
        handler = type('Handler', (), {})()
        handler.wfile = __import__('io').BytesIO()
        handler.connection = Mock()
        stream = adapter.Stream(handler, native.MODEL)
        stream.last_heartbeat -= 11
        with patch.object(adapter.select, 'select', return_value=([], [], [])):
            self.assertFalse(stream.cancelled())
        raw = handler.wfile.getvalue().decode()
        self.assertIn('event: response.in_progress', raw)
        self.assertNotIn(': keepalive', raw)


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
        return self.runtime.infer(tid, request, lambda text, kind='message': None, lambda: False)

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

    def test_progress_shows_thinking_actions_and_interim_text_once(self):
        seen = []
        _, usage = self.runtime.infer(self.thread, self.request('STREAM'),
                                      lambda text, kind='message': seen.append((kind, text)), lambda: False)
        # Codex reads input tokens as context in use: the last API call, never the turn's sum.
        self.assertEqual(usage, {'input_tokens': 5, 'cache_read_input_tokens': 40000,
                                 'cache_creation_input_tokens': 100, 'output_tokens': 7})
        self.assertEqual(seen, [
            ('status', 'Reading fixture.txt'),       # live line while tools run
            ('message', 'Read `fixture.txt`'),       # one compact line per burst of tool calls
            ('thinking', 'Plan: '), ('thinking', 'read files'),
            ('thinking_done', ''),                   # deltas already carried the text; no quote block
            ('message', 'Looking around.'),          # interim text, flushed by the next tool call
            ('status', 'Running npm test'),
            ('status', 'Scan: Searching for TODO'),  # subagent activity only in the live line
            ('message', 'Blocked by permissions: Write'),
            ('message', 'Ran `npm test`'),
            ('message', 'Agent **Scan** worked: searched for `TODO`')])  # final text is the answer
        args = json.loads((self.root / 'calls.jsonl').read_text())['args']
        self.assertEqual(args[args.index('--thinking-display') + 1], 'summarized')
        self.assertIn('--include-partial-messages', args)
        self.assertEqual(args[args.index('--setting-sources') + 1], 'user')

    def test_subagent_lifecycle_and_codex_run_tools(self):
        seen = []
        self.runtime.infer(self.thread, self.request('AGENTS'), lambda text, kind='message': seen.append((kind, text)),
                           lambda: False)
        self.assertEqual(seen, [
            ('status', 'Reading fixture.txt'),
            ('status', 'Starting agent Audit billing'),
            ('status', 'Audit billing: Reading refunds.ts'),
            # A relayed call is Codex's own row: earlier progress is emitted before it.
            ('message', 'Read `fixture.txt`, started agent **Audit billing**'),
            ('message', 'Checking the page.'),
            ('message', 'Agent **Audit billing** finished: read `refunds.ts`, ran `wc -l a.py`')])

    def test_burst_summary_uses_codex_wording(self):
        tally = native.Tally()
        for call in [('read', 'Read', 'a.ts'), ('read', 'Read', 'a.ts'), ('read', 'Read', 'b.ts'),
                     ('command', 'Bash', 'npm test'), ('search', 'Grep', 'TODO'), ('search', 'Grep', 'FIXME'),
                     ('agent', 'Agent', 'Audit billing'), ('other', 'TodoWrite', None)]:
            tally.add(*call)
        self.assertEqual(tally.summary(), 'Read 2 files, ran 2 searches, ran `npm test`, '
                                          'started agent **Audit billing**, used TodoWrite')

    def test_turn_after_codex_compaction_resumes_the_claude_session(self):
        self.run_turn(self.thread, self.request('first'))
        session = json.loads((self.runtime.directory / (self.thread + '.json')).read_text())['session_id']
        compacted = {**self.request('x'), 'input': [  # Codex rebuilt its history: no digest match
            {'role': 'user', 'content': 'first (retained by Codex)'},
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'summary ' + native.session_marker(session)}]},
            {'role': 'user', 'content': 'after compaction'}]}
        self.run_turn(self.thread, compacted)
        call = json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])
        self.assertEqual(call['args'][call['args'].index('--resume') + 1], session)
        self.assertEqual(call['request']['input'], [{'role': 'user', 'content': 'after compaction'}])

    def test_stopped_turn_resumes_its_claude_session(self):
        self.run_turn(self.thread, self.request('first'))
        path = self.runtime.directory / (self.thread + '.json')
        state = json.loads(path.read_text())
        state.update(status='interrupted', started=True, request='other')
        native.atomic_json(path, state)
        second = self.request('first')
        second['input'] = [*second['input'], {'role': 'user', 'content': 'next'}]
        self.run_turn(self.thread, second)
        call = json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])
        self.assertEqual(call['args'][call['args'].index('--resume') + 1], state['session_id'])

    def test_compaction_marker_resumes_even_after_a_model_switch(self):
        self.run_turn(self.thread, self.request('first'))
        session = json.loads((self.runtime.directory / (self.thread + '.json')).read_text())['session_id']
        self.runtime.bind(self.thread, self.root, model=native.FABLE)
        compacted = {**self.request('x'), 'model': native.FABLE, 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': native.session_marker(session)}]},
            {'role': 'user', 'content': 'after compaction'}]}
        self.run_turn(self.thread, compacted)
        call = json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])
        self.assertEqual(call['args'][call['args'].index('--resume') + 1], session)
        self.assertEqual(call['args'][call['args'].index('--model') + 1], native.FABLE)

    def test_reported_context_stays_under_codex_compaction(self):
        _, usage = self.runtime.infer(self.thread, self.request('HUGE STREAM'), lambda text, kind='message': None, lambda: False)
        self.assertEqual(usage['input_tokens'], native.REPORTED_CONTEXT_LIMIT)

    def test_run_not_on_subscription_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'apiKeySource=ANTHROPIC_API_KEY'):
            self.run_turn(self.thread, self.request('BADKEY'))

    def test_fresh_session_skips_quoted_thinking_but_keeps_progress(self):
        other = str(uuid.uuid4())
        self.runtime.bind(other, self.root)
        thinking = {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
                    'content': [{'type': 'output_text', 'text': native.THINKING_QUOTE + '\n>\n> secret plan'}]}
        progress = {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
                    'content': [{'type': 'output_text', 'text': 'Ran `ls`'}]}
        request = self.request('fresh')
        request['input'] = [*request['input'], {'type': 'reasoning', 'id': 'rs_1', 'summary': []}, thinking, progress]
        self.run_turn(other, request)
        sent = json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])['request']
        self.assertFalse(sent['continuation'])
        self.assertEqual([i.get('type') or i.get('role') for i in sent['input']], ['user', 'message'])
        self.assertEqual(sent['input'][1]['content'][0]['text'], 'Ran `ls`')

    def test_resumed_turn_does_not_replay_streamed_progress(self):
        first = self.request('first')
        self.run_turn(self.thread, first)
        progress = [{'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'content': None, 'encrypted_content': None},
                    {'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': [{'type': 'output_text', 'text': 'Ran `ls`'}]},
                    {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'CLAUDE_FIXTURE'}]}]
        second = {**first, 'input': [*first['input'], *progress, {'role': 'user', 'content': 'next'}]}
        self.run_turn(self.thread, second)
        sent = json.loads((self.root / 'calls.jsonl').read_text().splitlines()[-1])['request']
        self.assertTrue(sent['continuation'])
        self.assertEqual([i.get('phase') or i.get('role') for i in sent['input']], ['final_answer', 'user'])

    def test_permission_modes_and_read_only_side_chats(self):
        threads = []
        for permission in ('bypassPermissions', 'manual'):
            threads.append(str(uuid.uuid4()))
            self.runtime.bind(threads[-1], self.root, permission=permission)
        threads.append(str(uuid.uuid4()))
        self.runtime.bind(threads[-1], self.root, readonly=True, permission='bypassPermissions')
        for tid in threads:
            self.run_turn(tid, self.request(tid))
        calls = [v['args'] for v in map(json.loads, (self.root / 'calls.jsonl').read_text().splitlines())]
        self.assertEqual([a[a.index('--permission-mode') + 1] for a in calls], ['bypassPermissions', 'manual', 'auto'])
        # Manual without a way to ask the user denies prompts instead of guessing.
        self.assertTrue(all(a[a.index('--permission-prompts') + 1] == 'none' for a in calls))
        with self.assertRaises(ValueError):
            self.runtime.bind(self.thread, self.root, permission='dangerously-anything')

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
