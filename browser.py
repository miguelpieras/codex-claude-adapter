"""Turn-scoped MCP access to the desktop's configured computer-use runtime.

No browser profile, cookie store, debugging port or credential is accessed here.
The bundled app-server performs the call with the real task attribution.
"""
import asyncio
import copy
import json
import secrets
import threading
from native import MODEL, MODELS


class BrowserBridge:
    def __init__(self, adapter, loop, port):
        self.adapter, self.loop = adapter, loop
        self.url = f'http://127.0.0.1:{port}/mcp/browser'
        self.guard = threading.Lock()
        self.sessions = {}

    def run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout=95)
        except BaseException:
            future.cancel()
            raise

    def open(self, thread_id, metadata, *, model=MODEL):
        """Called off the app-server event loop, once per native Claude turn."""
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if not isinstance(metadata, dict):
            return None
        if metadata.get('auto_review_enabled') or metadata.get('node_repl_auto_review_required'):
            # Respect a host requirement for model-based review without routing
            # any Opus work to an OpenAI reviewer or disabling that requirement.
            return None
        session = self.run(self.discover(thread_id))
        if session is None:
            return None
        if metadata.get('thread_id', metadata.get('session_id')) != thread_id or metadata.get('turn_id') != session['turn']:
            raise ValueError('Browser metadata does not match the active Codex task and turn.')
        if not isinstance(metadata.get('session_id'), str) or model not in MODELS or metadata.get('model', model) != model:
            raise ValueError('Browser attribution is incomplete or references another model.')
        # Preserve core policy fields. Supply the actual model where older core
        # versions omit it; never impersonate an OpenAI model for browser checks.
        session['metadata'] = {**copy.deepcopy(metadata), 'model': model}
        token = secrets.token_urlsafe(32)
        with self.guard:
            self.sessions[token] = session
        config = {'mcpServers': {'codex_browser': {
            'type': 'http', 'url': self.url,
            'headers': {'Authorization': 'Bearer ' + token}}}}
        return token, config

    async def discover(self, tid):
        turn = self.adapter.active.get(tid)
        binding = self.adapter.native.bindings.get(tid)
        if not turn or not binding or binding['readonly']:
            return None
        cursor = None
        while True:
            params = {'threadId': tid, 'detail': 'toolsAndAuthOnly', 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            result = await self.adapter.core.call('mcpServerStatus/list', params)
            for server in result['data']:
                if (server['name'] != 'cua_repl' or
                        not (server.get('pluginId') or '').startswith('unified-computer-use@')):
                    continue
                tools = []
                for name in ('js', 'js_reset'):
                    tool = server.get('tools', {}).get(name)
                    if not tool:
                        continue
                    tool = {key: copy.deepcopy(tool[key]) for key in
                            ('name', 'description', 'inputSchema', 'title') if key in tool}
                    tool['name'] = name
                    # Browser JavaScript can change pages. Do not present it to
                    # Claude's permission system as an inherently read-only tool.
                    tool['annotations'] = {'readOnlyHint': False, 'openWorldHint': True}
                    tools.append(tool)
                if any(t['name'] == 'js' for t in tools):
                    return {'thread': tid, 'turn': turn, 'tools': tools}
            cursor = result.get('nextCursor')
            if not cursor:
                return None

    def close(self, token):
        with self.guard:
            self.sessions.pop(token, None)

    def cancel(self, thread_id):
        with self.guard:
            self.sessions = {k: v for k, v in self.sessions.items() if v['thread'] != thread_id}

    def tool_names(self, token):
        with self.guard:
            session = self.sessions.get(token)
        return {t['name'] for t in session['tools']} if session else set()

    async def call(self, token, name, arguments):
        with self.guard:
            session = self.sessions.get(token)
        if not session or self.adapter.active.get(session['thread']) != session['turn']:
            raise ValueError('This Claude browser turn has ended.')
        binding = self.adapter.native.bindings.get(session['thread'])
        if not binding or binding['readonly']:
            raise ValueError('Browser access is unavailable in read-only tasks.')
        if name not in {t['name'] for t in session['tools']}:
            raise ValueError('Only the configured Codex browser tools are exposed.')
        if not isinstance(arguments, dict):
            raise ValueError('Tool arguments must be an object.')
        # A bounded call may still finish after Stop; never retry it automatically.
        if name == 'js':
            if set(arguments) - {'code', 'title', 'timeout_ms'} or not isinstance(arguments.get('code'), str):
                raise ValueError('Invalid browser JavaScript arguments.')
            arguments = dict(arguments)
            timeout = arguments.get('timeout_ms', 30000)
            if type(timeout) is not int or not 1 <= timeout <= 60000:
                raise ValueError('Browser timeout_ms must be between 1 and 60000.')
            arguments['timeout_ms'] = timeout
        elif arguments:
            raise ValueError('Reset takes no arguments.')
        result = await self.adapter.core.call('mcpServer/tool/call', {
            'threadId': session['thread'], 'server': 'cua_repl', 'tool': name,
            'arguments': arguments,
            '_meta': {'x-codex-turn-metadata': session['metadata']}})
        return {k: v for k, v in result.items() if v is not None}

    def handle(self, handler):
        """Minimal stateless Streamable HTTP MCP transport, private bearer token."""
        def send(status, data=None):
            raw = b'' if data is None else json.dumps(data).encode()
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(raw)))
            handler.end_headers()
            if raw:
                handler.wfile.write(raw)

        authorization = handler.headers.get('Authorization', '')
        token = authorization.removeprefix('Bearer ') if authorization.startswith('Bearer ') else ''
        with self.guard:
            session = self.sessions.get(token)
        if not session or handler.headers.get('Origin'):
            return send(403)
        if handler.command == 'DELETE':
            self.close(token)
            return send(200)
        if handler.command != 'POST':
            return send(405)
        ident = None
        try:
            length = int(handler.headers.get('Content-Length', '0'))
            if not 0 < length <= 1_000_000:
                raise ValueError('Invalid MCP request size.')
            request = json.loads(handler.rfile.read(length))
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                raise ValueError('Invalid MCP request.')
            ident = request.get('id')
            method, params = request.get('method'), request.get('params') or {}
            if method == 'notifications/initialized':
                return send(202)
            if method == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'codex-claude-browser', 'version': '1.0.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': session['tools']}
            elif method == 'tools/call':
                try:
                    result = self.run(self.call(token, params.get('name'), params.get('arguments', {})))
                except Exception as error:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)[:1200]}]}
            else:
                return send(200, {'jsonrpc': '2.0', 'id': ident,
                                  'error': {'code': -32601, 'message': 'Unsupported MCP method.'}})
            # Preserve MCP image blocks and host errors without text/base64 coercion.
            return send(200, {'jsonrpc': '2.0', 'id': ident, 'result': result})
        except (ValueError, TypeError, AttributeError):
            return send(400, {'jsonrpc': '2.0', 'id': ident,
                              'error': {'code': -32600, 'message': 'Invalid MCP request.'}})
