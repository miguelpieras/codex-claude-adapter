#!/usr/bin/env python3
"""An isolated Claude desktop mode; no app-server interposition or app patches."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from urllib.parse import urlparse

from adapter import make_catalog, Core
from native import MODEL, MODELS, atomic_json, check_auth
from paths import APP, CODEX, require_codex
from standalone import PROVIDER, service
import app_icon

ROOT = Path(__file__).resolve().parent
HOME = Path.home() / '.codex'
DIRECTORY = Path.home() / '.codex-claude'


def private_directory(path):
    if path.is_symlink():
        raise RuntimeError('Refusing a symlinked standalone directory.')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def owned(directory):
    if directory.is_symlink() or directory.resolve() in (Path.home(), HOME.resolve(), ROOT):
        raise RuntimeError('Refusing to manage an unrelated directory.')
    marker = directory / 'owner.json'
    if not marker.is_file() or json.loads(marker.read_text()) != {'provider': PROVIDER, 'version': 1}:
        raise RuntimeError('This directory is not marked as owned by standalone Claude.')


def app_running(directory):
    require_codex()
    if app_icon.state(directory) is not None and app_icon.is_running(app_icon.bundle(directory)):
        return True
    result = subprocess.run(['/bin/ps', '-axo', 'args='], capture_output=True, text=True, check=True)
    target = str(APP) + ' --user-data-dir=' + str(directory / 'desktop')
    return any(line.strip() == target or line.strip().startswith(target + ' ')
               for line in result.stdout.splitlines())


def endpoint(directory):
    path = directory / 'endpoint.json'
    return json.loads(path.read_text()) if path.exists() else None


def control(directory, path='/status'):
    state = endpoint(directory)
    if not state:
        return None
    request = urllib.request.Request('http://127.0.0.1:' + str(state['port']) + path,
        headers={'X-Local-Claude-Token': state['token']},
        method='POST' if path == '/stop' else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.loads(response.read()) if response.status != 204 else {'stopped': True}
    except (OSError, ValueError, urllib.error.URLError):
        return None


def prepare(directory=DIRECTORY):
    """Create only owned Claude state. Never copy credentials or native history."""
    require_codex()
    if len(os.fsencode(directory.resolve() / 'home' / 'ipc' / 'ipc.sock')) >= 104:
        raise RuntimeError('Claude state path is too long for macOS sockets. Use ~/.codex-claude.')
    if directory.resolve() in (Path.home(), HOME.resolve(), ROOT):
        raise RuntimeError('Use a dedicated standalone directory.')
    private_directory(directory)
    marker = directory / 'owner.json'
    if marker.exists():
        owned(directory)
    else:
        existing = directory / 'home' / 'config.toml'
        contents = {v.name for v in directory.iterdir()} - {'service.lock', 'service.log'}
        if contents and (not existing.is_file() or tomllib.loads(existing.read_text()).get('model_provider') != PROVIDER):
            raise RuntimeError('Refusing to use an existing unrelated directory.')
        atomic_json(marker, {'provider': PROVIDER, 'version': 1})
    home = directory / 'home'
    private_directory(home)
    private_directory(directory / 'desktop')
    catalog = home / 'claude-models.json'
    make_catalog(HOME, catalog)
    data = json.loads(catalog.read_text())
    data['models'] = [v for v in data['models'] if v['slug'] in MODELS]
    for model in data['models']:
        model['input_modalities'] = ['text', 'image']
        model['supports_image_detail_original'] = True
        model['base_instructions'] = ('You are Claude using local Claude Code. Use native tools and native '
            'Agent subagents. Explicitly exposed Codex browser and task coordination MCP tools are available. '
            'Do not use an OpenAI model or API or an Anthropic API key. Respect the supplied permissions.')
    atomic_json(catalog, data)
    return home


def configure(home, state):
    """Own the provider block while retaining user preferences in this profile."""
    config = home / 'config.toml'
    existing = config.read_text() if config.exists() else ''
    settings = tomllib.loads(existing) if existing else {}
    if settings.get('model_provider', PROVIDER) != PROVIDER:
        raise RuntimeError('Claude profile now selects another provider; refusing to overwrite it.')
    if not existing:
        existing = ('model=' + json.dumps(MODEL) + '\nmodel_provider=' + json.dumps(PROVIDER) +
            '\nmodel_reasoning_effort="medium"\nmodel_supports_reasoning_summaries=false\n'
            'approval_policy="on-request"\napprovals_reviewer="user"\nsandbox_mode="workspace-write"\n'
            'web_search="disabled"\nmodel_catalog_json=' + json.dumps(str(home / 'claude-models.json')) + '\n'
            '\n[analytics]\nenabled=false\n')
        for plugin in ('codex-app-tools', 'unified-computer-use', 'browser', 'chrome'):
            existing += '\n[plugins.' + json.dumps(plugin + '@openai-bundled') + ']\nenabled=true\n'
    # Provider configuration is at the end of the generated file. App writes
    # may reformat/reorder it, so update through the bundled config API below
    # on subsequent launches rather than replacing unrelated profile fields.
    provider = {'name': 'Local Claude subscription',
        'base_url': 'http://127.0.0.1:' + str(state['port']) + '/v1', 'wire_api': 'responses',
        'requires_openai_auth': False, 'supports_websockets': False,
        'request_max_retries': 0, 'stream_max_retries': 0,
        'http_headers': {'X-Local-Claude-Token': state['token']}}
    if config.exists():
        write_provider(home, provider)
    else:
        existing += '\n[model_providers.' + PROVIDER + ']\n'
        for key, value in provider.items():
            if isinstance(value, dict):
                continue
            existing += key + '=' + json.dumps(value) + '\n'
        existing += '\n[model_providers.' + PROVIDER + '.http_headers]\nX-Local-Claude-Token=' + json.dumps(state['token']) + '\n'
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(existing)


def write_provider(home, provider):
    require_codex()
    async def update():
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_CLI_PATH'}
        env['CODEX_HOME'] = str(home)
        process = await asyncio.create_subprocess_exec(str(CODEX), 'app-server', '--stdio',
            env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=16_000_000)
        core = Core(process, lambda _: None)
        try:
            await core.call('initialize', {'clientInfo': {'name': 'claude_mode_config', 'version': '1.0'}})
            await core.send({'method': 'initialized'})
            await core.call('config/value/write', {'keyPath': 'model_providers.' + PROVIDER,
                'value': provider, 'mergeStrategy': 'replace'})
        finally:
            process.stdin.close()
            await asyncio.wait_for(process.wait(), 15)
    asyncio.run(update())


def serve(directory=DIRECTORY):
    private_directory(directory)
    with (directory / 'service.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        home = prepare(directory)
        # Keep the endpoint stable across service restarts. An already-open
        # desktop may retain the provider configuration until its next reload.
        config = home / 'config.toml'
        options = {}
        if config.exists():
            provider = tomllib.loads(config.read_text()).get('model_providers', {}).get(PROVIDER, {})
            if provider:
                url = urlparse(provider.get('base_url', ''))
                token = provider.get('http_headers', {}).get('X-Local-Claude-Token')
                if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path != '/v1' or not isinstance(token, str) or len(token) < 32:
                    raise RuntimeError('Invalid existing standalone endpoint; refusing to replace it.')
                options = {'port': url.port, 'token': token}
        server = service(directory / 'sessions', home=home, **options)
        state = {'port': server.server_port, 'token': server.token, 'pid': os.getpid()}
        configure(home, state)
        atomic_json(directory / 'endpoint.json', state)
        def stop(*_):
            server.native.close()
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            server.serve_forever()
        finally:
            server.native.close()
            deadline = time.monotonic() + 5
            for turn in list(server.turns.values()):
                turn.done.wait(max(0, deadline - time.monotonic()))
            server.server_close()
            (directory / 'endpoint.json').unlink(missing_ok=True)


def start(directory=DIRECTORY):
    if control(directory):
        return
    check_auth(ROOT)
    prepare(directory)
    with (directory / 'service.log').open('a') as log:
        process = subprocess.Popen([sys.executable, str(ROOT / 'claude_mode.py'), 'serve', '--directory', str(directory)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if control(directory):
            return
        if process.poll() is not None:
            raise RuntimeError('Claude service failed to start. See ' + str(directory / 'service.log'))
        time.sleep(.1)
    raise RuntimeError('Claude service did not become ready.')


def launch(directory=DIRECTORY):
    # A healthy, pre-update service does not prove the installed app still
    # exists at a supported path. Validate before start() can return early.
    require_codex()
    app = app_icon.select(APP, directory)
    start(directory)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'OPENAI_', 'ANTHROPIC_'))}
    env.update(CODEX_HOME=str(directory / 'home'), CODEX_ELECTRON_USER_DATA_PATH=str(directory / 'desktop'))
    # The official app already supports these isolated data paths. No CLI shim
    # or replacement binary is involved; both app and its core stay signed.
    subprocess.Popen([str(app), '--user-data-dir=' + str(directory / 'desktop')],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    print('Opened isolated Claude mode. Regular Codex settings and tasks are unchanged.')


def remove(directory=DIRECTORY, *, delete_history=False):
    if directory.exists():
        owned(directory)
        if app_running(directory):
            raise RuntimeError('Close the Claude-mode window first. Regular Codex may remain open.')
        control(directory, '/stop')
        # The lock is released only after native sessions and the server exit.
        with (directory / 'service.lock').open('a+') as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Claude service is still stopping; data was retained.')
                    time.sleep(.1)
            from dock import remove as remove_launcher
            remove_launcher(root=ROOT)
            app_icon.remove(directory)
            if delete_history:
                shutil.rmtree(directory)
    else:
        from dock import remove as remove_launcher
        remove_launcher(root=ROOT)
    print('Standalone adapter stopped and its launcher removed. Regular Codex is unchanged.')
    print('Claude-mode history deleted.' if delete_history else 'Claude-mode history retained at ' + str(directory))


def standard(directory=DIRECTORY):
    require_codex()
    import manage
    env = {k: v for k, v in os.environ.items() if not k.startswith(('CODEX_', 'OPENAI_', 'ANTHROPIC_'))}
    if manage.candidates(HOME):
        # Only old wrapper installations need provider migration. The new
        # isolated mode never changes a regular Codex task's provider.
        subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'rollback'], env=env, check=True)
    control(directory, '/stop')
    subprocess.Popen([str(APP)], env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print('Opened standard Codex. Claude service stopped; Claude-mode history retained.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'start', 'serve', 'launch', 'stop', 'status', 'remove', 'standard'])
    parser.add_argument('--directory', type=Path, default=DIRECTORY)
    parser.add_argument('--delete-history', action='store_true', help='With remove only: permanently delete isolated Claude-mode history/settings.')
    args = parser.parse_args()
    args.directory = args.directory.expanduser().absolute()
    if args.delete_history and args.action != 'remove':
        parser.error('--delete-history requires remove')
    if args.action == 'remove':
        remove(args.directory, delete_history=args.delete_history)
    elif args.action == 'stop':
        print(json.dumps(control(args.directory, '/stop') or {'stopped': True}))
    elif args.action == 'status':
        print(json.dumps(control(args.directory) or {'running': False}))
    else:
        globals()[args.action](args.directory)


if __name__ == '__main__':
    main()
