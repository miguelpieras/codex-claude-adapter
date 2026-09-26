import plistlib
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
