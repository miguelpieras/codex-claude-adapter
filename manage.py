#!/usr/bin/env python3
"""Launch, restore, and remove this opt-in adapter without changing app files."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tomllib

from adapter import CODEX, HOME, ROOT, RUNTIME, Core
from native import MODEL, PROVIDER, atomic_json, check_auth
from paths import APP, require_codex



def app_running():
    require_codex()
    result = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True)
    if result.returncode or result.stderr.strip():
        raise RuntimeError('Cannot verify whether Codex is running. No restoration or removal was attempted.')
    return str(APP) in (line.strip() for line in result.stdout.splitlines())


def candidates(home=HOME):
    database = home / 'state_5.sqlite'
    if not database.exists():
        return []
    with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(
            'SELECT id, model_provider, model, archived FROM threads WHERE model_provider = ?',
            (PROVIDER,))]



async def migrate(home=HOME, runtime=RUNTIME):
    path = runtime / 'restore.json'
    originals = json.loads(path.read_text()) if path.exists() else {}
    cfg = tomllib.loads((home / 'config.toml').read_text()) if (home / 'config.toml').exists() else {}
    fallback = cfg.get('model', 'gpt-6-astra')
    if fallback == MODEL:
        fallback = 'gpt-6-astra'
    rows = candidates(home)
    env = dict(os.environ, CODEX_HOME=str(home))
    process = await asyncio.create_subprocess_exec(str(CODEX), 'app-server', '--stdio',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, limit=16_000_000)
    core = Core(process, lambda _: None)
    restored = []
    try:
        await core.call('initialize', {'clientInfo': {'name': 'local_claude_rollback', 'version': '1.0'},
                                        'capabilities': {'experimentalApi': True}})
        await core.send({'method': 'initialized'})
        for row in rows:
            tid = row['id']
            target = originals.get(tid, {'model': fallback, 'provider': 'openai'})
            if row['archived']:
                await core.call('thread/unarchive', {'threadId': tid})
            try:
                options = {'threadId': tid, 'model': target['model'],
                    'modelProvider': target['provider'], 'excludeTurns': True}
                if target.get('effort'):
                    options['config'] = {'model_reasoning_effort': target['effort']}
                result = await core.call('thread/resume', options)
                if result['modelProvider'] != target['provider'] or result['model'] != target['model']:
                    raise RuntimeError('Task restoration could not be verified: ' + tid)
                await core.call('thread/unsubscribe', {'threadId': tid})
                restored.append(tid)
                originals.pop(tid, None)
            finally:
                if row['archived']:
                    await core.call('thread/archive', {'threadId': tid})
        if candidates(home):
            raise RuntimeError('Some saved tasks still reference the adapter. Removal was stopped.')
        if path.exists():
            atomic_json(path, {})
    finally:
        process.terminate()
        await process.wait()
    return restored



def rollback(*, remove=False):
    if app_running():
        raise RuntimeError('Quit Codex first so running tasks are not interrupted. Then run this command again.')
    if RUNTIME.is_symlink():
        raise RuntimeError('Runtime directory is a symlink; no changes were made.')
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (RUNTIME / 'owner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('An adapter process is still running. Let Codex finish quitting first.') from None
        restored = asyncio.run(migrate())
        if remove:
            # Only generated runtime state is owned by the adapter. Never delete
            # a source checkout, its .git history, or a sibling directory.
            known = {'owner.lock', 'restore.json', 'preferences.json', 'models.json', 'sessions'}
            unknown = {p.name for p in RUNTIME.iterdir()} - known
            if unknown or RUNTIME.is_symlink():
                raise RuntimeError('Unexpected runtime files or a symlink; task restoration succeeded, but automatic deletion was stopped.')
            shutil.rmtree(RUNTIME)
    print('Native provider restored for ' + str(len(restored)) + ' saved task(s). Global Codex settings and app bundle are unchanged.')
    if remove:
        print('Adapter runtime removed. This source checkout is now safe to delete. Claude Code and all conversation history are retained.')


def launch():
    if app_running():
        raise RuntimeError('Quit Codex first, then open Start Codex with Opus.command. No tasks were stopped.')
    check_auth(ROOT)
    # Repair stale task metadata from an earlier interrupted adapter run.
    rollback()
    env = dict(os.environ, CODEX_CLI_PATH=str(ROOT / 'codex-with-claude'),
               CODEX_ADAPTER_PYTHON=str(Path(sys.executable).resolve()))
    subprocess.Popen([str(APP)], env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print('Started Codex with the optional local Claude adapter.')


def standard():
    rollback()
    env = {k: v for k, v in os.environ.items() if k != 'CODEX_CLI_PATH'}
    subprocess.Popen([str(APP)], env=env, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    print('Started the standard Codex app.')


def status():
    cfg = tomllib.loads((HOME / 'config.toml').read_text()) if (HOME / 'config.toml').exists() else {}
    print(json.dumps({'app_running': app_running(), 'global_provider': cfg.get('model_provider', 'openai'),
                      'global_model': cfg.get('model'), 'tasks_needing_adapter': len(candidates()),
                      'runtime_directory': str(RUNTIME)}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['launch', 'standard', 'rollback', 'uninstall', 'status'])
    args = parser.parse_args()
    try:
        if args.action == 'uninstall':
            rollback(remove=True)
        else:
            globals()[args.action]()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
