"""Standalone subscription-only Responses provider with a native Claude MCP relay.

An explicitly bound task keeps one native Claude process while Codex executes
ordinary Responses function calls. There is no app-server RPC proxy, raw desktop
socket access, or model API client. Only host function tools are supported here.
"""
import asyncio
from collections import deque
from contextlib import closing
import hmac
import json
from pathlib import Path
import queue
import secrets
import sqlite3
import threading
import time
import uuid
from http.server import ThreadingHTTPServer

import native
from adapter import Handler, Stream
from browser import BrowserBridge

PROVIDER = 'claude-standalone'
COORDINATION = {'list_threads', 'read_thread', 'wait_threads', 'send_message_to_thread'}
SYSTEM = native.SYSTEM.replace('Other Codex app connectors and\nvoice are unavailable.',
    'Explicitly exposed Codex task coordination MCP tools are also available. Voice is unavailable.').replace(
    'different provider, explain that they must switch this task\'s dropdown.',
    'different provider, explain that regular Codex is a separate launch mode.')


def task_record(home, tid):
    database = Path(home) / 'state_5.sqlite'
    if not database.is_file():
        return None
    with closing(sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT cwd, model, model_provider FROM threads WHERE id = ?', (tid,)).fetchone()
        return dict(row) if row else None


def binding_for(home, tid, model, metadata):
    """Use authenticated core metadata and its local task registry, never prompts."""
    if model not in native.MODELS or metadata.get('model') != model or metadata.get('thread_id') != tid:
        raise ValueError('Mismatched task/model attribution; no fallback.')
    uuid.UUID(tid)
    uuid.UUID(metadata.get('turn_id', ''))
    if metadata.get('auto_review_enabled') is not False or metadata.get('node_repl_auto_review_required') is not False:
        raise ValueError('Claude mode does not support "Approve for me": it hands approvals to a Codex reviewer model. '
                         'Choose "Ask for approval" (Claude Auto mode) or "Full access" (Claude bypass mode).')
    mode = metadata.get('sandbox_mode')
    if mode not in ('read-only', 'workspace-write', 'danger-full-access'):
        raise ValueError('Missing or unsupported host permissions.')
    row = task_record(home, tid)
    if row:
        if row['model_provider'] != PROVIDER:
            raise ValueError('This task does not belong to the standalone Claude provider.')
        cwd = row['cwd']
    else:
        # Ephemeral side chats have no persistent task row. The host supplies
        # workspace metadata separately from conversation text. Ambiguity fails.
        parent_id = metadata.get('forked_from_thread_id')
        parent = task_record(home, parent_id) if isinstance(parent_id, str) else None
        roots = list((metadata.get('workspaces') or {}).keys())
        if parent:
            if mode != 'read-only' or parent['model_provider'] != PROVIDER or parent['model'] != model:
                raise ValueError('Read-only side chats must retain their parent Claude model/provider.')
            cwd = parent['cwd']
        elif len(roots) == 1:
            cwd = roots[0]
        else:
            raise ValueError('An ephemeral task must identify exactly one workspace.')
    effort = metadata.get('reasoning_effort')
    native.claude_effort(model, effort)
    return dict(cwd=str(Path(cwd).resolve(strict=True)), readonly=mode == 'read-only', model=model, effort=effort,
                full_access=mode == 'danger-full-access')


def descriptors(tools, namespace=None):
    result = {}
    for tool in tools:
        if tool.get('type') == 'namespace':
            result.update(descriptors(tool.get('tools', []), tool['name']))
        elif tool.get('type') == 'function':
            # Narrow proof: browser + task tools. Native Claude keeps all its
            # own execution tools and subagents. No host subagent forwarding.
            if (namespace == 'mcp__cua_repl' and tool['name'] in ('js', 'js_reset') or
                namespace == 'mcp__codex_app' and tool['name'] in COORDINATION):
                ident = (namespace + '__' if namespace else '') + tool['name']
                result[ident] = {**tool, 'namespace': namespace}
    return result


def mcp_result(output):
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get('content'), list):
            return parsed
        return {'content': [{'type': 'text', 'text': output}]}
    blocks = []
    for part in output if isinstance(output, list) else [output]:
        if isinstance(part, dict) and part.get('type') == 'input_image':
            url = part.get('image_url', '')
            if not url.startswith('data:image/') or ';base64,' not in url:
                raise ValueError('Only inline host images are supported; remote images are never fetched.')
            header, encoded = url.split(';base64,', 1)
            blocks.append({'type': 'image', 'mimeType': header[5:], 'data': encoded})
        elif isinstance(part, dict) and part.get('type') in ('input_text', 'output_text', 'text'):
            blocks.append({'type': 'text', 'text': part.get('text', '')})
        else:
            blocks.append({'type': 'text', 'text': json.dumps(part)})
    return {'content': blocks}


class Turn:
    def __init__(self, tid, request, metadata):
        self.tid, self.model, self.metadata = tid, request['model'], metadata
        self.tools = descriptors(request.get('tools', []))
        self.events, self.outputs = queue.Queue(), queue.Queue()
        self.stopped = threading.Event()
        self.http_lock, self.tool_lock = threading.Lock(), threading.Lock()
        self.pending = None
        self.completed = False
        self.done = threading.Event()
        self.last_input = request.get('input', [])
        self.last_request = native.digest(request)


class Relay(BrowserBridge):
    def __init__(self, server):
        self.server = server
        self.url = f'http://127.0.0.1:{server.server_port}/mcp/browser'
        self.guard, self.sessions = threading.Lock(), {}

    def run(self, coro):
        return asyncio.run(coro)

    def open(self, thread_id, metadata, *, model):
        turn = self.server.turns[thread_id]
        if model != turn.model or metadata != turn.metadata:
            raise ValueError('Mismatched native turn attribution.')
        token = secrets.token_urlsafe(32)
        tools = [{'name': name, 'description': tool.get('description', ''),
                  'inputSchema': tool['parameters'],
                  'annotations': {'readOnlyHint': False, 'openWorldHint': True}}
                 for name, tool in turn.tools.items()]
        with self.guard:
            self.sessions[token] = {'thread': thread_id, 'turn': turn, 'tools': tools}
        return token, {'mcpServers': {'codex_browser': {
            'type': 'http', 'url': self.url,
            'headers': {'Authorization': 'Bearer ' + token}}}}

    async def call(self, token, name, arguments):
        with self.guard:
            session = self.sessions.get(token)
        if not session:
            raise ValueError('Expired native turn.')
        turn = session['turn']
        if name not in turn.tools or not isinstance(arguments, dict):
            raise ValueError('Unavailable host tool or invalid arguments.')
        # One outstanding host call per task; separate tasks run concurrently.
        with turn.tool_lock:
            if turn.stopped.is_set():
                raise ValueError('Cancelled task.')
            tool = turn.tools[name]
            if tool['name'] == 'send_message_to_thread':
                target = arguments.get('threadId')
                if arguments.get('hostId', 'local') != 'local':
                    raise ValueError('Remote message model/provider pinning is unavailable.')
                if arguments.get('model', turn.model) != turn.model:
                    raise ValueError('Messages must preserve the selected Claude model.')
                row = task_record(self.server.home, target)
                if target != turn.tid and (not row or row['model_provider'] != PROVIDER or row['model'] != turn.model):
                    raise ValueError('Message target must already use the same Claude model/provider. No OpenAI task will be started.')
            item = {'type': 'function_call', 'id': 'fc_' + uuid.uuid4().hex,
                    'call_id': 'call_' + uuid.uuid4().hex, 'status': 'completed',
                    'name': tool['name'], 'arguments': json.dumps(arguments)}
            if tool.get('namespace'):
                item['namespace'] = tool['namespace']
            turn.pending = item['call_id']
            turn.events.put(('call', item))
            deadline = time.monotonic() + 120
            while not turn.stopped.is_set() and time.monotonic() < deadline:
                try:
                    output = turn.outputs.get(timeout=.25)
                    turn.pending = None
                    result = mcp_result(output)
                    self.server.observations.append({'tool': name, 'returned': True,
                        'content_types': [v.get('type') for v in result.get('content', [])]})
                    return result
                except queue.Empty:
                    pass
            turn.stopped.set()
            raise ValueError('Host continuation missing or cancelled; no retry.')

    def cancel(self, tid):
        if tid in self.server.turns:
            self.server.turns[tid].stopped.set()
        super().cancel(tid)


class ServiceHandler(Handler):
    def do_GET(self):
        if self.path == '/status' and hmac.compare_digest(self.headers.get('X-Local-Claude-Token', ''), self.server.token):
            raw = json.dumps({'provider': PROVIDER, 'pid': __import__('os').getpid(),
                              'running_tasks': len(self.server.native.running)}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        super().do_GET()

    def do_POST(self):
        if self.path == '/mcp/browser':
            return self.server.browser.handle(self)
        if self.headers.get('Origin') or not hmac.compare_digest(self.headers.get('X-Local-Claude-Token', ''), self.server.token):
            return self.reject(403, 'Unauthorized local client.')
        if self.path == '/stop':
            self.send_response(204)
            self.end_headers()
            self.server.native.close()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != '/v1/responses':
            return self.reject(404, 'This provider supports Claude task responses only. Voice is unavailable.')
        turn = None
        locked = False
        streaming = False
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8_000_000:
                raise ValueError('Invalid request size.')
            request = json.loads(self.rfile.read(size))
            tid = self.headers.get('thread-id', '')
            uuid.UUID(tid)
            metadata = json.loads((request.get('client_metadata') or {}).get('x-codex-turn-metadata') or '{}')
            binding = binding_for(self.server.home, tid, request.get('model'), metadata)
            if not isinstance(request.get('input'), list) or request.get('previous_response_id'):
                raise ValueError('Full task context is required.')
            request_digest = native.digest(request)
            state_path = self.server.native.directory / (tid + '.json')
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            if state.get('status') == 'completed' and state.get('response_request_digest') == request_digest:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                Stream(self, request['model']).finish(state['result'], state.get('usage', {}))
                return
            self.server.requests.append({'model': request['model'], 'thread': tid})
            with self.server.guard:
                turn = self.server.turns.get(tid)
                fresh = turn is None or turn.completed
                if turn and turn.stopped.is_set():
                    if not turn.done.is_set():
                        raise ValueError('Previous Claude process is still stopping. Retry after it exits.')
                    fresh = True
                if fresh:
                    self.server.native.bind(tid, **binding)
                    turn = Turn(tid, request, metadata)
                    self.server.turns[tid] = turn
                elif turn.metadata != metadata or turn.model != request['model']:
                    raise ValueError('Continuation changed task attribution or model.')
                locked = turn.http_lock.acquire(blocking=False)
                if not locked:
                    raise ValueError('Concurrent HTTP request for one task.')
            if not fresh:
                matches = [item for item in request.get('input', []) if item.get('type') == 'function_call_output' and item.get('call_id') == turn.pending]
                if len(matches) != 1 or not turn.pending or turn.stopped.is_set():
                    raise ValueError('Missing exact host result; automatic replay refused.')
                turn.outputs.put(matches[0]['output'])
            turn.last_input = request['input']
            turn.last_request = native.digest(request)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Connection', 'close')
            self.end_headers()
            streaming = True
            stream = Stream(self, turn.model)
            if fresh:
                def worker():
                    try:
                        final, usage = self.server.native.infer(tid, request,
                            lambda text: turn.events.put(('message', text)), turn.stopped.is_set,
                            browser_metadata=metadata)
                        turn.events.put(('final', (final, usage)))
                    except Exception as error:
                        turn.events.put(('error', str(error)))
                    finally:
                        turn.done.set()
                threading.Thread(target=worker, daemon=True).start()
            while True:
                if stream.cancelled():
                    raise ValueError('Disconnected client; native turn cancelled.')
                try:
                    kind, value = turn.events.get(timeout=.25)
                except queue.Empty:
                    continue
                if kind == 'message':
                    stream.message(value)
                elif kind == 'call':
                    index = len(stream.response['output'])
                    stream.event('response.output_item.added', output_index=index, item={**value, 'status': 'in_progress', 'arguments': ''})
                    stream.event('response.function_call_arguments.delta', item_id=value['id'], output_index=index, delta=value['arguments'])
                    stream.event('response.function_call_arguments.done', item_id=value['id'], output_index=index, arguments=value['arguments'])
                    stream.event('response.output_item.done', output_index=index, item=value)
                    stream.response['output'].append(value)
                    stream.response.update(status='completed')
                    stream.event('response.completed', response=stream.response)
                    break
                elif kind == 'final':
                    # Native MCP already delivered the host results. Advance the
                    # context checkpoint so a subsequent user turn does not
                    # present the same host call/results as fresh work.
                    path = self.server.native.directory / (tid + '.json')
                    state = json.loads(path.read_text())
                    state.update(input_count=len(turn.last_input), input_digest=native.digest(turn.last_input),
                                 response_request_digest=turn.last_request)
                    native.atomic_json(path, state)
                    stream.finish(*value)
                    turn.completed = True
                    break
                else:
                    raise ValueError(value)
        except Exception as error:
            if turn and locked:
                turn.stopped.set()
                self.server.native.cancel(turn.tid)
            if streaming:
                try:
                    stream.event('error', error={'code': 'claude_service_error', 'message': str(error)[:1200]})
                except OSError:
                    pass
            else:
                self.reject(400, str(error)[:1200])
        finally:
            if locked:
                turn.http_lock.release()
            self.close_connection = True


def service(directory, *, home, token=None, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), ServiceHandler)
    server.daemon_threads = True
    server.home = Path(home)
    server.token = token or secrets.token_urlsafe(32)
    server.turns = {}
    server.requests, server.observations = deque(maxlen=100), deque(maxlen=100)
    server.guard = threading.Lock()
    server.native = native.NativeRuntime(directory, system=SYSTEM, inline_images=True)
    server.browser = Relay(server)
    server.native.browser = server.browser
    return server
