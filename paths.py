"""Locate user-installed applications; no vendor binaries are bundled."""
import os
from pathlib import Path
import plistlib
import shutil


def find_app():
    explicit = os.environ.get('CODEX_ADAPTER_APP')
    candidates = [Path(explicit).expanduser()] if explicit else [
        base / name for base in (Path('/Applications'), Path.home() / 'Applications')
        for name in ('Codex.app', 'ChatGPT.app')]
    for path in candidates:
        try:
            info = plistlib.loads((path / 'Contents/Info.plist').read_bytes())
            # The regular ChatGPT app is a different product, even when the
            # Codex desktop distribution happens to be named ChatGPT.app.
            if info.get('CFBundleIdentifier') != 'com.openai.codex':
                continue
            executable = info.get('CFBundleExecutable', '')
            app = path / 'Contents/MacOS' / executable
            codex = path / 'Contents/Resources/codex'
            if executable and app.is_file() and codex.is_file():
                return app.resolve(), codex.resolve()
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
    return None, None


def find_claude():
    explicit = os.environ.get('CODEX_ADAPTER_CLAUDE')
    if explicit:
        return Path(explicit).expanduser().resolve()
    local = Path.home() / '.local/bin/claude'
    found = shutil.which('claude')
    return local if local.is_file() else Path(found) if found else local


APP, CODEX = find_app()
CLAUDE = find_claude()


def require_codex():
    if APP is None or CODEX is None:
        raise RuntimeError('Codex desktop was not found. Set CODEX_ADAPTER_APP to its .app path. The ordinary ChatGPT app is not supported.')
