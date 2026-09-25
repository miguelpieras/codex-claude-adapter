#!/usr/bin/env python3
"""Build a local Dock applet. No changes to the installed Codex application."""
import argparse
import fcntl
import json
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

from adapter import HOME, ROOT, RUNTIME
from paths import APP, require_codex

NAME = 'Codex with Opus'
IDENTIFIER = 'local.codex-claude-adapter.launcher'


def bundle(root=ROOT):
    return root / (NAME + '.app')


def verify_owned(path, root):
    if path.is_symlink():
        raise RuntimeError('The launcher is a symlink; it was left unchanged.')
    info = plistlib.loads((path / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != IDENTIFIER or info.get('CodexAdapterRoot') != str(root):
        raise RuntimeError('An unrelated app occupies the launcher path; it was left unchanged.')


def dock_entries():
    result = subprocess.run(['/usr/bin/defaults', 'export', 'com.apple.dock', '-'],
                            check=True, capture_output=True)
    return plistlib.loads(result.stdout).get('persistent-apps', [])


def is_our_tile(entry, path):
    url = entry.get('tile-data', {}).get('file-data', {}).get('_CFURLString', '')
    return url.rstrip('/') == path.as_uri().rstrip('/')


def refresh_dock():
    # Restart only the Dock; applications and their running tasks stay open.
    result = subprocess.run(['/usr/bin/killall', 'Dock'], capture_output=True)
    if result.returncode not in (0, 1):
        raise RuntimeError('Launcher saved, but the Dock could not be refreshed. Log in again to refresh it.')


def install():
    require_codex()
    path = bundle()
    if path.exists() or path.is_symlink():
        verify_owned(path, ROOT)
    command = shlex.join(['/usr/bin/env', 'CODEX_HOME=' + str(HOME),
                         sys.executable, str(ROOT / 'dock.py'), 'launch'])
    script = ('on run\ntry\ndo shell script ' + json.dumps(command, ensure_ascii=False) +
              '\non error messageText\ndisplay alert "' + NAME +
              '" message messageText as warning buttons {"OK"} default button "OK"\nend try\nend run\n')
    subprocess.run(['/usr/bin/osacompile', '-o', str(path), '-'], input=script,
                   text=True, check=True, capture_output=True)
    info_path = path / 'Contents/Info.plist'
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleIdentifier=IDENTIFIER, CFBundleDisplayName=NAME,
                CFBundleName=NAME, CodexAdapterRoot=str(ROOT), LSUIElement=True)
    info_path.write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)],
                   check=True, capture_output=True)
    if not any(is_our_tile(e, path) for e in dock_entries()):
        tile = {'tile-type': 'file-tile', 'tile-data': {'file-label': NAME,
                'file-data': {'_CFURLString': path.as_uri(), '_CFURLStringType': 15}}}
        subprocess.run(['/usr/bin/defaults', 'write', 'com.apple.dock', 'persistent-apps',
                        '-array-add', plistlib.dumps(tile).decode()], check=True, capture_output=True)
        refresh_dock()
    if not any(is_our_tile(e, path) for e in dock_entries()):
        raise RuntimeError('The app was built, but its Dock entry could not be verified.')
    print('Prepared and pinned: ' + str(path))


def remove(root=ROOT):
    path = bundle(root)
    if not path.exists() and not path.is_symlink():
        return
    verify_owned(path, root)
    entries = dock_entries()
    remaining = [e for e in entries if not is_our_tile(e, path)]
    if remaining != entries:
        if dock_entries() != entries:
            raise RuntimeError('Dock changed concurrently; retry removal.')
        subprocess.run(['/usr/bin/defaults', 'write', 'com.apple.dock', 'persistent-apps',
                        '-array', *[plistlib.dumps(e).decode() for e in remaining]],
                       check=True, capture_output=True)
        refresh_dock()
        if any(is_our_tile(e, path) for e in dock_entries()):
            raise RuntimeError('Dock removal could not be verified; the app was retained.')
    shutil.rmtree(path)


def launch():
    import manage
    if manage.app_running():
        lock = RUNTIME / 'owner.lock'
        if lock.exists():
            with lock.open('r') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # This checkout already owns the live adapter. Activate it
                    # without restarting, migrating tasks or spawning another app.
                    subprocess.run(['/usr/bin/open', '-a', str(APP.parents[2])], check=True)
                    return
        raise RuntimeError('Codex is already open without this adapter. Let your tasks finish, '
                           'quit Codex completely, then click Codex with Opus. Nothing was stopped.')
    manage.launch()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'launch', 'remove'])
    args = parser.parse_args()
    try:
        globals()[args.action]()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
