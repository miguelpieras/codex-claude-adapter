"""Optional Finder icon on a local copy; never edit the installed signed app."""
import hashlib
import json
import plistlib
import shutil
import subprocess
from pathlib import Path

from native import atomic_json

OWNER = 'claude-mode-finder-icon-v1'


def bundle(directory):
    return directory / 'appearance' / 'Claude Codex.app'


def executable(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    name = info.get('CFBundleExecutable', '')
    if info.get('CFBundleIdentifier') != 'com.openai.codex' or not name or Path(name).name != name:
        raise RuntimeError('The icon copy is not a supported Codex app.')
    return app / 'Contents/MacOS' / name


def fingerprint(app):
    digest = hashlib.sha256()
    for path in (app / 'Contents/Info.plist', app / 'Contents/_CodeSignature/CodeResources', executable(app)):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def state(directory):
    root = directory / 'appearance'
    if not root.exists() and not root.is_symlink():
        return None
    if root.is_symlink() or bundle(directory).is_symlink():
        raise RuntimeError('Refusing a symlinked icon-copy directory.')
    marker = root / 'owner.json'
    value = json.loads(marker.read_text()) if marker.is_file() else {}
    if value.get('owner') != OWNER:
        raise RuntimeError('The appearance directory is unrelated; it was left unchanged.')
    return value


def is_running(app):
    if not app.exists():
        return False
    target = str(executable(app))
    result = subprocess.run(['/bin/ps', '-axo', 'args='], capture_output=True, text=True, check=True)
    return any(line.strip() == target or line.strip().startswith(target + ' ')
               for line in result.stdout.splitlines())


def verify(app):
    # Finder custom-icon metadata fails --strict's metadata check. The normal
    # deep check still validates the original signed code and nested helpers.
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', str(app)],
                   check=True, capture_output=True)


def select(original, directory):
    saved = state(directory)
    if saved is None:
        return original
    app = bundle(directory)
    source = original.parent.parent.parent
    if app.is_dir() and saved.get('source') == fingerprint(source) == fingerprint(app):
        verify(app)
        return executable(app)
    if is_running(app):
        raise RuntimeError('Codex updated. Close the Claude window before reopening its launcher.')
    print('Codex updated: using the current official app. Re-enable the running icon to recolor it.')
    return original


def install(original, directory, icon):
    source = original.parent.parent.parent
    icon_bytes = Path(icon).expanduser().read_bytes()
    if not icon_bytes.startswith(b'icns'):
        raise ValueError('Choose a macOS .icns icon.')
    state(directory)  # Refuse unrelated existing data before making changes.
    app = bundle(directory)
    if is_running(app):
        raise RuntimeError('Close the Claude icon-copy window before changing its icon.')
    verify(source)
    source_hash = fingerprint(source)
    app.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_json(app.parent / 'owner.json', {'owner': OWNER, 'source': source_hash})
    if app.exists():
        shutil.rmtree(app)
    subprocess.run(['/bin/cp', '-cRp', str(source), str(app)], check=True)
    verify(app)
    if fingerprint(app) != source_hash or fingerprint(source) != source_hash:
        raise RuntimeError('Codex changed while copying. Retry after its update finishes.')
    icon_file = app.parent / 'icon.icns'
    icon_file.write_bytes(icon_bytes)
    script = ('ObjC.import("AppKit"); function run(a) { '
              'const icon = $.NSImage.alloc.initWithContentsOfFile(a[1]); '
              'if (!icon || !$.NSWorkspace.sharedWorkspace.setIconForFileOptions(icon, a[0], 0)) '
              'throw Error("Finder icon customization failed"); }')
    subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script, str(app), str(icon_file)], check=True)
    verify(app)
    if fingerprint(app) != source_hash:
        raise RuntimeError('The signed app contents changed unexpectedly.')


def remove(directory):
    if state(directory) is None:
        return
    if is_running(bundle(directory)):
        raise RuntimeError('Close the Claude icon-copy window before removing it.')
    from dock import remove_tile
    remove_tile(bundle(directory))
    shutil.rmtree(bundle(directory).parent)


if __name__ == '__main__':
    import argparse
    import claude_mode
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'remove'])
    parser.add_argument('--icon', type=Path)
    args = parser.parse_args()
    claude_mode.prepare()
    if args.action == 'install':
        if args.icon is None:
            parser.error('install requires --icon /path/to/icon.icns')
        if claude_mode.app_running(claude_mode.DIRECTORY):
            raise RuntimeError('Close the Claude-mode window first; regular Codex may stay open.')
        install(claude_mode.APP, claude_mode.DIRECTORY, args.icon)
        print('Running icon prepared. Open Claude through its existing launcher.')
    else:
        remove(claude_mode.DIRECTORY)
        print('Running icon removed. Claude will use the original app again; history retained.')
