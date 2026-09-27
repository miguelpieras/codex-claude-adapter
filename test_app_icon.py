import plistlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app_icon
from native import atomic_json


class AppIconTests(unittest.TestCase):
    def fixture(self, root):
        app = root / 'Official.app'
        (app / 'Contents/MacOS').mkdir(parents=True)
        (app / 'Contents/_CodeSignature').mkdir()
        (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': 'com.openai.codex', 'CFBundleExecutable': 'Codex'}))
        (app / 'Contents/_CodeSignature/CodeResources').write_text('signed resources')
        (app / 'Contents/MacOS/Codex').write_text('signed executable')
        return app

    def test_updates_use_official_app_with_running_copy_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.fixture(root)
            directory = root / 'profile'
            copied = app_icon.bundle(directory)
            copied.parent.mkdir(parents=True)
            import shutil
            shutil.copytree(original, copied)
            atomic_json(copied.parent / 'owner.json', {
                'owner': app_icon.OWNER, 'source': app_icon.fingerprint(original)})
            with patch.object(app_icon, 'verify'):
                self.assertEqual(app_icon.select(app_icon.executable(original), directory), app_icon.executable(copied))
                (original / 'Contents/MacOS/Codex').write_text('new official version')
                with patch.object(app_icon, 'is_running', return_value=True):
                    with self.assertRaisesRegex(RuntimeError, 'Close'):
                        app_icon.select(app_icon.executable(original), directory)
                with patch.object(app_icon, 'is_running', return_value=False):
                    self.assertEqual(app_icon.select(app_icon.executable(original), directory), app_icon.executable(original))

    def test_removal_preserves_history_and_refuses_unowned_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            history = directory / 'history.txt'
            history.write_text('keep')
            appearance = directory / 'appearance'
            appearance.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'unrelated'):
                app_icon.remove(directory)
            self.assertTrue(appearance.exists())
            atomic_json(appearance / 'owner.json', {'owner': app_icon.OWNER})
            with patch.object(app_icon, 'is_running', return_value=True):
                with self.assertRaisesRegex(RuntimeError, 'Close'):
                    app_icon.remove(directory)
            with patch.object(app_icon, 'is_running', return_value=False), patch('dock.remove_tile'):
                app_icon.remove(directory)
            self.assertFalse(appearance.exists())
            self.assertEqual(history.read_text(), 'keep')


class AppIconUpdateTests(unittest.TestCase):
    fixture = AppIconTests.fixture

    def test_update_rebuilds_the_orange_copy_and_drops_the_updaters_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.fixture(root)
            directory = root / 'profile'
            (directory / 'appearance').mkdir(parents=True)
            atomic_json(directory / 'appearance' / 'owner.json', {'owner': app_icon.OWNER, 'source': 'before-update'})
            (directory / 'appearance' / 'icon.icns').write_bytes(b'icns fixture')
            renamed = directory / 'appearance' / 'ChatGPT.app'  # what Codex's updater left behind
            shutil.copytree(original, renamed)
            rebuilt = lambda source, target, icon: shutil.copytree(original, app_icon.bundle(target))
            with patch.object(app_icon, 'install', side_effect=rebuilt) as install, \
                    patch.object(app_icon, 'verify'), patch.object(app_icon, 'is_running', return_value=False), \
                    patch('dock.dock_entries', return_value=[]):
                chosen = app_icon.select(app_icon.executable(original), directory)
            self.assertEqual(chosen, app_icon.executable(app_icon.bundle(directory)))
            self.assertFalse(renamed.exists())
            self.assertEqual(install.call_args.args[2], directory / 'appearance' / 'icon.icns')

    def test_update_never_removes_a_running_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.fixture(root)
            directory = root / 'profile'
            (directory / 'appearance').mkdir(parents=True)
            atomic_json(directory / 'appearance' / 'owner.json', {'owner': app_icon.OWNER, 'source': 'before-update'})
            (directory / 'appearance' / 'icon.icns').write_bytes(b'icns fixture')
            renamed = directory / 'appearance' / 'ChatGPT.app'
            shutil.copytree(original, renamed)
            with patch.object(app_icon, 'install') as install, patch.object(app_icon, 'verify'), \
                    patch.object(app_icon, 'is_running', side_effect=lambda app: app == renamed):
                chosen = app_icon.select(app_icon.executable(original), directory)
            self.assertEqual(chosen, app_icon.executable(original))
            self.assertTrue(renamed.exists())
            install.assert_not_called()
