#!/usr/bin/env python3
"""Opt-in Codex CLI wrapper. Native traffic stays in the bundled Codex binary."""
import asyncio
import copy
import hmac
import json
import os
from pathlib import Path
import secrets
import select
import socket
import sys
import threading
import time
import tomllib
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from native import MODEL, MODELS, EFFORTS, claude_effort, PROVIDER, NativeRuntime, atomic_json, digest
from paths import CODEX, require_codex
from browser import BrowserBridge

ROOT = Path(__file__).resolve().parent
HOME = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
RUNTIME = HOME / 'claude-adapter'


def make_catalog(home, destination):
    source = home / 'models_cache.json'
    config = tomllib.loads((home / 'config.toml').read_text()) if (home / 'config.toml').exists() else {}
    if config.get('model_catalog_json'):
        source = Path(config['model_catalog_json'])
    models = json.loads(source.read_text())['models']
    models = [m for m in models if m['slug'] not in MODELS]
    entries = []
    for model, (name, default_effort) in MODELS.items():
        entry = copy.deepcopy(next((m for m in models if m['slug'] == 'gpt-5.5'), models[0]))
        entry.update(slug=model, display_name=name.removeprefix('Claude '),
            description='Local Claude Code login. Ultra selects Claude Ultracode workflows.',
            priority=0, upgrade=None, available_access_programs={'cyber': []},
            additional_speed_tiers=[], service_tiers=[], context_window=100000,
            max_context_window=100000, comp_hash=None, input_modalities=['text'],
            supports_image_detail_original=False, supports_search_tool=False,
            support_verbosity=False, node_repl_disabled=False, use_responses_lite=False,
            default_reasoning_level=default_effort, experimental_supported_tools=[],
            supported_reasoning_levels=[{'effort': effort, 'description':
                'Claude Ultracode: Extra High reasoning with automatic dynamic workflows' if effort == 'ultra'
                else 'Claude Code ' + effort + ' reasoning'} for effort in EFFORTS])
        for key in ('model_messages', 'tool_mode', 'multi_agent_reasoning_effort'):
            entry.pop(key, None)
        entry['base_instructions'] = ('You are Claude running through local Claude Code. '
            'Follow developer and user instructions and the provided project guidance. '
            'Execute using native Claude Code tools and explicitly exposed browser MCP tools. '
            'Other Codex host tool schemas are unavailable. '
            "Respect permissions, existing changes, and the user's requested scope.")
        entries.append(entry)
    atomic_json(destination, {'models': [*entries, *models]})


class Stream:
    def __init__(self, handler, model):
        self.handler = handler
        self.response = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response',
                         'created_at': int(time.time()), 'model': model, 'output': [],
                         'status': 'in_progress', 'usage': None, 'error': None}
        self.sequence = 0
        self.last_heartbeat = time.monotonic()
        self.open = None  # streaming reasoning or action-list item, closed before any other item
        self.event('response.created', response=self.response)

    def emit(self, text, kind='message'):
        """Native progress: 'message' commentary, 'action' list line, 'thinking' delta, 'thinking_done' full text."""
        if kind == 'action':
            if not self.open or self.open['kind'] != 'action':
                self.close_open()
                self.start('action', {'type': 'message', 'id': 'msg_' + uuid.uuid4().hex, 'role': 'assistant',
                                      'phase': 'commentary', 'status': 'in_progress', 'content': []})
                self.event('response.content_part.added', **self.open['common'],
                           part={'type': 'output_text', 'text': '', 'annotations': []})
            self.delta(text + '\n')
        elif kind in ('thinking', 'thinking_done'):
            reasoning_open = bool(self.open) and self.open['kind'] == 'reasoning'
            if kind == 'thinking_done' and not text and not reasoning_open:
                return  # hidden thinking: an empty reasoning item would render nothing
            if not reasoning_open:
                self.close_open()
                self.start('reasoning', {'type': 'reasoning', 'id': 'rs_' + uuid.uuid4().hex, 'summary': []})
                self.event('response.reasoning_summary_part.added', **self.open['common'],
                           part={'type': 'summary_text', 'text': ''})
            if kind == 'thinking':
                self.delta(text)
            else:
                # Add only what the live deltas missed; never repeat streamed text.
                if text.startswith(self.open['text']) and len(text) > len(self.open['text']):
                    self.delta(text[len(self.open['text']):])
                self.close_open()
        else:
            self.close_open()
            self.message(text)

    def start(self, kind, item):
        index = len(self.response['output'])
        common = {'item_id': item['id'], 'output_index': index}
        common.update(summary_index=0) if kind == 'reasoning' else common.update(content_index=0)
        self.open = {'kind': kind, 'item': item, 'index': index, 'common': common, 'text': ''}
        self.event('response.output_item.added', output_index=index, item=item)

    def delta(self, text):
        self.open['text'] += text
        name = 'response.reasoning_summary_text.delta' if self.open['kind'] == 'reasoning' else 'response.output_text.delta'
        self.event(name, **self.open['common'], delta=text)

    def close_open(self):
        if not self.open:
            return
        state, self.open = self.open, None
        text, common, index = state['text'], state['common'], state['index']
        if state['kind'] == 'reasoning':
            part = {'type': 'summary_text', 'text': text}
            self.event('response.reasoning_summary_text.done', **common, text=text)
            self.event('response.reasoning_summary_part.done', **common, part=part)
            item = {**state['item'], 'summary': [part]}
        else:
            part = {'type': 'output_text', 'text': text.rstrip('\n'), 'annotations': []}
            self.event('response.output_text.done', **common, text=part['text'])
            self.event('response.content_part.done', **common, part=part)
            item = {**state['item'], 'status': 'completed', 'content': [part]}
        self.event('response.output_item.done', output_index=index, item=item)
        self.response['output'].append(item)

    def event(self, kind, **data):
        data.update(type=kind, sequence_number=self.sequence)
        self.sequence += 1
        self.handler.wfile.write(('event: ' + kind + '\ndata: ' + json.dumps(data) + '\n\n').encode())
        self.handler.wfile.flush()

    def message(self, text, phase='commentary'):
        index = len(self.response['output'])
        item = {'type': 'message', 'id': 'msg_' + uuid.uuid4().hex, 'role': 'assistant',
                'phase': phase, 'status': 'completed',
                'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
        self.event('response.output_item.added', output_index=index,
                   item={**item, 'status': 'in_progress', 'content': []})
        common = {'item_id': item['id'], 'output_index': index, 'content_index': 0}
        self.event('response.content_part.added', **common, part={'type': 'output_text', 'text': '', 'annotations': []})
        self.event('response.output_text.delta', **common, delta=text)
        self.event('response.output_text.done', **common, text=text)
        self.event('response.content_part.done', **common, part=item['content'][0])
        self.event('response.output_item.done', output_index=index, item=item)
        self.response['output'].append(item)

    def cancelled(self):
        conn = self.handler.connection
        if select.select([conn], [], [], 0)[0] and not conn.recv(1, socket.MSG_PEEK):
            return True
        if time.monotonic() - self.last_heartbeat > 10:
            # Codex's idle timer (300s) ignores SSE comments; only real events reset it.
            self.event('response.in_progress', response={'id': self.response['id'], 'object': 'response',
                                                          'status': 'in_progress'})
            self.last_heartbeat = time.monotonic()
        return False

    def finish(self, text, usage):
        self.close_open()
        self.message(text, 'final_answer')
        incoming = sum(usage.get(k, 0) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        outgoing = usage.get('output_tokens', 0)
        self.response.update(status='completed', usage={'input_tokens': incoming, 'output_tokens': outgoing,
            'total_tokens': incoming + outgoing, 'input_tokens_details': {'cached_tokens': usage.get('cache_read_input_tokens', 0)},
            'output_tokens_details': {'reasoning_tokens': 0}})
        self.event('response.completed', response=self.response)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reject(self, status, message):
        raw = json.dumps({'error': {'message': message}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        if self.path == '/mcp/browser' and getattr(self.server, 'browser', None):
            return self.server.browser.handle(self)
        if not hmac.compare_digest(self.headers.get('X-Local-Claude-Token', ''), self.server.token):
            return self.reject(401, 'Unrecognized local adapter client.')
        if self.path != '/v1/responses':
            return self.reject(400, 'This Claude adapter supports task text inference only; this endpoint is unavailable.')
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= 8_000_000:
                raise ValueError('Request too large or empty.')
            data = json.loads(self.rfile.read(length))
            if data.get('model') not in MODELS:
                raise ValueError('Only supported Claude models are allowed. No OpenAI fallback exists in this backend.')
            thread_id = self.headers.get('thread-id', '')
            uuid.UUID(thread_id)
        except (ValueError, TypeError):
            return self.reject(400, 'Invalid Claude task request; no model was called.')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        self.end_headers()
        stream = Stream(self, data['model'])
        try:
            metadata = self.headers.get('x-codex-turn-metadata') or (data.get('client_metadata') or {}).get('x-codex-turn-metadata')
            # This legacy wrapper shares ~/.codex tasks with OpenAI models, which must never
            # receive locally made reasoning items; thinking reaches it as quoted commentary only.
            emit = lambda text, kind='message': None if kind in ('thinking', 'thinking_done') else stream.emit(text, kind)
            final, usage = self.server.native.infer(thread_id, data, emit, stream.cancelled,
                                                   browser_metadata=metadata)
            stream.finish(final, usage)
        except (BrokenPipeError, ConnectionResetError):
            self.server.native.cancel(thread_id)
        except Exception as error:
            try:
                stream.close_open()
                stream.event('error', error={'code': 'claude_code_error', 'message': str(error)[:1200]})
            except OSError:
                pass
        self.close_connection = True

    def do_GET(self):
        if self.path == '/mcp/browser' and getattr(self.server, 'browser', None):
            return self.server.browser.handle(self)
        self.reject(404, 'Unknown adapter endpoint.')

    do_DELETE = do_GET


class RpcError(Exception):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get('message', str(error)))


class Core:
    def __init__(self, process, emit):
        self.process, self.emit = process, emit
        self.pending = {}
        self.serial = 0
        self.reader = asyncio.create_task(self.read())

    async def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                ident = message.get('id')
                future = self.pending.pop(ident, None) if 'method' not in message else None
                if future is not None:
                    if not future.done():
                        if 'error' in message:
                            future.set_exception(RpcError(message['error']))
                        else:
                            future.set_result(message.get('result'))
                else:
                    self.emit(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError('Bundled Codex app-server exited.'))

    async def call(self, method, params=None):
        self.serial += 1
        ident = 'local-claude-rpc-' + str(self.serial)
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        await self.send({'id': ident, 'method': method, 'params': params or {}})
        return await asyncio.wait_for(future, 90)


class Adapter:
    def __init__(self, home, runtime, native, emit):
        self.home, self.runtime, self.native, self.emit = home, runtime, native, emit
        self.core = None
        self.threads, self.settings, self.locks, self.active = {}, {}, {}, {}
        self.restore_path = runtime / 'restore.json'
        self.restore = json.loads(self.restore_path.read_text()) if self.restore_path.exists() else {}
        self.preference_path = runtime / 'preferences.json'
        self.preferences = json.loads(self.preference_path.read_text()) if self.preference_path.exists() else {}
        cfg = tomllib.loads((home / 'config.toml').read_text()) if (home / 'config.toml').exists() else {}
        self.default_model = cfg.get('model', 'gpt-6-astra')
        self.default_effort = cfg.get('model_reasoning_effort')
        self.default_provider = cfg.get('model_provider', 'openai')
        if self.default_provider not in ('openai', 'fixture_native'):
            raise ValueError('Restore the native global provider before using this adapter.')

    def notification(self, message):
        p = message.get('params', {})
        method = message.get('method')
        if method == 'thread/started':
            self.threads[p['thread']['id']] = p['thread']
        if method == 'turn/started':
            self.active[p['threadId']] = p['turn']['id']
        if method == 'turn/completed':
            self.active.pop(p['threadId'], None)
        self.emit(message)

    async def metadata(self, tid):
        result = await self.core.call('thread/read', {'threadId': tid, 'includeTurns': False})
        self.threads[tid] = result['thread']
        return result['thread']

    def remember(self, thread, model=None):
        if thread.get('ephemeral'):
            return
        tid = thread['id']
        if tid not in self.restore:
            current = model or thread.get('model') or self.default_model
            self.restore[tid] = {'model': current if current not in MODELS else self.default_model,
                                 'provider': self.default_provider,
                                 'effort': thread.get('reasoningEffort') or self.default_effort}
            atomic_json(self.restore_path, self.restore)

    def bind(self, result):
        thread = result['thread']
        thread.update(model=result['model'], modelProvider=result['modelProvider'])
        self.threads[thread['id']] = thread
        self.settings[thread['id']] = result
        if result['modelProvider'] == PROVIDER:
            self.native.bind(thread['id'], result['cwd'], readonly=result.get('sandbox', {}).get('type') == 'readOnly', model=result['model'], effort=result.get('reasoningEffort'))
        return result

    async def unload(self, tid):
        # Loaded-list includes cached runtimes even after their subscriber is gone.
        # Unsubscribe then resume is the supported reload boundary. Verify the
        # resulting provider before sending any turn, rather than polling caches.
        await self.core.call('thread/unsubscribe', {'threadId': tid})

    async def switch(self, tid, model, effort=None):
        thread = await self.metadata(tid)
        provider = PROVIDER if model in MODELS else self.default_provider
        if thread['modelProvider'] != provider:
            if tid in self.active or thread.get('status', {}).get('type') == 'active':
                raise ValueError('Wait for this task to finish or stop it before switching models.')
            if provider == PROVIDER:
                self.remember(thread)
            await self.unload(tid)
            options = {'threadId': tid, 'model': model, 'modelProvider': provider}
            if effort:
                options['config'] = {'model_reasoning_effort': effort}
            result = await self.core.call('thread/resume', options)
            if result['modelProvider'] != provider:
                raise ValueError('Provider switch did not take effect. The turn was blocked before inference.')
            self.bind(result)
        elif provider == PROVIDER and tid not in self.native.bindings:
            result = await self.core.call('thread/resume', {'threadId': tid, 'model': model, 'modelProvider': provider})
            self.bind(result)

    async def handle(self, method, params):
        params = copy.deepcopy(params or {})
        tid = params.get('threadId')
        if method in ('config/value/write', 'config/batchWrite'):
            return await self.write_config(method, params)
        if method == 'config/read':
            result = await self.core.call(method, params)
            if self.preferences.get('model') in MODELS:
                result['config'].update(self.preferences)
                for key in self.preferences:
                    result['origins'][key] = {'name': {'type': 'sessionFlags'}, 'version': digest(self.preferences)}
            return result
        if method == 'model/list':
            result = await self.core.call(method, params)
            if self.preferences.get('model') in MODELS:
                for entry in result['data']:
                    entry['isDefault'] = entry['model'] == self.preferences['model']
            return result
        if method == 'turn/interrupt' and tid:
            self.native.cancel(tid)
        if method.startswith('thread/realtime/') and tid:
            thread = await self.metadata(tid)
            if thread.get('modelProvider') == PROVIDER:
                raise ValueError('Voice uses OpenAI. It is disabled for Claude tasks; switch to a native Codex model to use voice.')
        if method in ('thread/start', 'thread/resume', 'thread/fork', 'thread/settings/update', 'turn/start'):
            lock = self.locks.setdefault(tid or uuid.uuid4().hex, asyncio.Lock())
            async with lock:
                return await self.route(method, params)
        return await self.core.call(method, params)

    async def write_config(self, method, params):
        edits = params.get('edits', [params])
        for edit in edits:
            if edit.get('keyPath') == 'model_provider' and edit.get('value') == PROVIDER:
                raise ValueError('Select Claude per task; the adapter cannot become the global Codex provider.')
        selected = next((e.get('value') for e in edits if e.get('keyPath') == 'model'), None)
        virtual = selected in MODELS or (selected is None and self.preferences.get('model') in MODELS)
        if virtual:
            for edit in edits:
                if edit.get('keyPath') in ('model_reasoning_effort', 'plan_mode_reasoning_effort'):
                    claude_effort(selected or self.preferences['model'], edit.get('value'))
        intercepted = [e for e in edits if virtual and e.get('keyPath') in ('model', 'model_reasoning_effort', 'plan_mode_reasoning_effort')]
        if not intercepted:
            result = await self.core.call(method, params)
            if selected and selected not in MODELS:
                self.preferences = {}
                atomic_json(self.preference_path, self.preferences)
                self.default_model = selected
            return result
        # Dropdown defaults for Opus live in this adapter's own file. They must
        # never strand standard Codex with model=Opus and provider=openai.
        config = await self.core.call('config/read', {'includeLayers': True})
        user = next((layer for layer in config.get('layers', [])
                     if layer['name']['type'] == 'user' and not layer['name'].get('profile')), None)
        if not user:
            raise ValueError('Cannot establish the user config version; no settings were changed.')
        if params.get('expectedVersion') and params['expectedVersion'] != user['version']:
            raise ValueError('Configuration changed concurrently; refresh settings before trying again.')
        remaining = [e for e in edits if e not in intercepted]
        if remaining:
            request = {k: v for k, v in params.items() if k not in ('edits', 'keyPath', 'value', 'mergeStrategy')}
            request['edits'] = remaining
            result = await self.core.call('config/batchWrite', request)
        else:
            result = {'status': 'ok', 'filePath': str(self.preference_path), 'version': user['version']}
        self.preferences.update({e['keyPath']: e['value'] for e in intercepted})
        atomic_json(self.preference_path, self.preferences)
        return result

    async def route(self, method, params):
        tid = params.get('threadId')
        model = params.get('model') or (params.get('collaborationMode') or {}).get('settings', {}).get('model')
        source = await self.metadata(tid) if tid else None
        if method == 'thread/fork' and not model:
            model = source.get('model')
        model = model or (source or {}).get('model') or self.preferences.get('model') or self.default_model
        if source and source.get('model') != model and (tid in self.active or source.get('status', {}).get('type') == 'active'):
            raise ValueError('Wait for this task to finish or stop it before switching models.')
        if model in MODELS:
            effort = (params.get('effort') or params.get('reasoningEffort') or
                      (params.get('config') or {}).get('model_reasoning_effort') or
                      (params.get('collaborationMode') or {}).get('settings', {}).get('reasoning_effort'))
            if effort is not None:
                claude_effort(model, effort)
        if method in ('turn/start', 'thread/settings/update'):
            await self.switch(tid, model)
            if model in MODELS:
                params['approvalsReviewer'] = 'user'
                current = self.settings[tid]
                profile = (current.get('activePermissionProfile') or {}).get('id')
                if params.get('permissions') and params['permissions'] != profile:
                    raise ValueError('Changing a named Codex permissions profile during a Claude task is unsupported. Select the profile before starting the task.')
                self.native.bind(tid, params.get('cwd') or current['cwd'], readonly=(
                    params.get('sandboxPolicy') or current.get('sandbox', {})).get('type') == 'readOnly', model=model,
                    effort=effort or current.get('reasoningEffort'))
            result = await self.core.call(method, params)
            if model in MODELS:
                current['model'] = model
                if effort:
                    current['reasoningEffort'] = effort
                self.threads[tid]['model'] = model
                if params.get('cwd'):
                    current['cwd'] = params['cwd']
                if params.get('sandboxPolicy'):
                    current['sandbox'] = params['sandboxPolicy']
            return result
        if method == 'thread/resume' and source['modelProvider'] != (PROVIDER if model in MODELS else self.default_provider):
            await self.switch(tid, model)
        if model in MODELS:
            params.update(model=model, modelProvider=PROVIDER)
            # Native Claude owns its own permissions; no Codex auto-review model.
            params['approvalsReviewer'] = 'user'
            if source:
                self.remember(source)
            if method == 'thread/fork' and params.get('ephemeral'):
                params['sandbox'] = 'read-only'
        elif params.get('modelProvider') == PROVIDER:
            params['modelProvider'] = self.default_provider
        result = self.bind(await self.core.call(method, params))
        if model in MODELS:
            self.remember(result['thread'])
        return result

    async def restore_all(self):
        errors = []
        for tid, original in list(self.restore.items()):
            try:
                await self.switch(tid, original['model'], original.get('effort'))
                thread = await self.metadata(tid)
                if thread.get('modelProvider') != original['provider']:
                    raise ValueError('Native provider restoration not confirmed.')
                self.restore.pop(tid)
                atomic_json(self.restore_path, self.restore)
            except Exception:
                errors.append(tid)
        return errors


async def serve(args):
    require_codex()
    if RUNTIME.is_symlink():
        raise RuntimeError('Runtime directory is a symlink; no changes were made.')
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An exclusive OS lock prevents two desktop wrappers owning the same sessions.
    import fcntl
    lockfile = (RUNTIME / 'owner.lock').open('a+')
    fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lockfile.seek(0)
    lockfile.truncate()
    lockfile.write(str(os.getpid()))
    lockfile.flush()
    native = NativeRuntime(RUNTIME / 'sessions')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.token, server.native = secrets.token_urlsafe(32), native
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    catalog = RUNTIME / 'models.json'
    make_catalog(HOME, catalog)
    provider = '{name="Local Claude Code", base_url="http://127.0.0.1:' + str(server.server_port) + '/v1", wire_api="responses", requires_openai_auth=false, supports_websockets=false, request_max_retries=0, stream_max_retries=0, stream_idle_timeout_ms=120000, http_headers={"X-Local-Claude-Token"=' + json.dumps(server.token) + '}}'
    extra = ['-c', 'model_providers.' + PROVIDER + '=' + provider,
             '-c', 'model_catalog_json=' + json.dumps(str(catalog))]
    output_alive = True
    def emit(message):
        nonlocal output_alive
        if output_alive:
            try:
                print(json.dumps(message), flush=True)
            except (BrokenPipeError, OSError):
                output_alive = False
    adapter = Adapter(HOME, RUNTIME, native, emit)
    proc = await asyncio.create_subprocess_exec(str(CODEX), *args, *extra, stdin=asyncio.subprocess.PIPE,
                                               stdout=asyncio.subprocess.PIPE, stderr=sys.stderr,
                                               limit=16_000_000)
    adapter.core = Core(proc, adapter.notification)
    native.browser = server.browser = BrowserBridge(adapter, asyncio.get_running_loop(), server.server_port)
    tasks = set()
    async def dispatch(message):
        if 'method' not in message or 'id' not in message:
            await adapter.core.send(message)
            return
        try:
            result = await adapter.handle(message['method'], message.get('params'))
            emit({'id': message['id'], 'result': result})
        except Exception as error:
            detail = error.error if isinstance(error, RpcError) else {'code': -32000, 'message': str(error)}
            emit({'id': message['id'], 'error': detail})
    reader = asyncio.StreamReader(limit=16_000_000)
    protocol = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        while line := await reader.readline():
            task = asyncio.create_task(dispatch(json.loads(line)))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        output_alive = False
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        native.close()
        for tid, turn in list(adapter.active.items()):
            if tid not in native.bindings:
                continue
            try:
                await adapter.core.call('turn/interrupt', {'threadId': tid, 'turnId': turn})
            except Exception:
                pass
        for _ in range(50):
            if not adapter.active:
                break
            await asyncio.sleep(0.1)
        failures = await adapter.restore_all()
        if failures:
            print('Claude adapter: run manage.py rollback before a standard launch; some tasks were not restored.', file=sys.stderr)
        proc.terminate()
        await proc.wait()
        server.shutdown()
        server.server_close()
        catalog.unlink(missing_ok=True)
        lockfile.close()


def wraps_server(args):
    """Only wrap the server itself, never its proxy/daemon/schema helper commands."""
    # Desktop browser and app-tools clients spawn this exact auxiliary form.
    # It must not claim the desktop adapter lock or install a model provider.
    # The desktop host uses --analytics-default-enabled; explicit adapter
    # protocol clients use --stdio instead.
    if args == ['app-server', '--listen', 'stdio://']:
        return False
    if 'app-server' not in args or any(v in args for v in ('--help', '-h', '--version')):
        return False
    after = iter(args[args.index('app-server') + 1:])
    values = {'-c', '--config', '--enable', '--disable', '--listen', '--code-mode-host',
              '--ws-auth', '--ws-token-file', '--ws-token-sha256', '--ws-shared-secret-file',
              '--ws-issuer', '--ws-audience', '--ws-max-clock-skew-seconds'}
    for value in after:
        if value in values:
            next(after, None)
        elif not value.startswith('-'):
            return False
    return True


if __name__ == '__main__':
    require_codex()
    if not wraps_server(sys.argv[1:]):
        os.execv(str(CODEX), [str(CODEX), *sys.argv[1:]])
    asyncio.run(serve(sys.argv[1:]))
