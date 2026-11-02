"""Regression coverage for repeated launches and reentrant scene operations."""
import importlib
from pathlib import Path
import runpy
import sys
import threading
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_import_state as state


STARTER = Path(__file__).resolve().parents[1] / 'tencent_fbx_importer_downscale.py'


def dialog(visible=True, running=False, minimized=False):
    result = Mock()
    result._running = running
    result.isVisible.return_value = visible
    result.isMinimized.return_value = minimized
    return result


class ImportSessionTests(unittest.TestCase):
    def setUp(self):
        self.assertFalse(state.is_busy())
        state.set_dialog(None)

    def tearDown(self):
        state.set_dialog(None)
        self.assertFalse(state.is_busy())

    def test_nested_direct_import_and_batch_are_rejected(self):
        with state.import_session():
            self.assertTrue(state.is_busy())
            with self.assertRaisesRegex(state.ImporterBusyError, 'arbeitet bereits'):
                with state.import_session():
                    self.fail('Nested import entered')
            with self.assertRaises(state.ImporterBusyError):
                with state.batch_session():
                    self.fail('Nested batch entered')

    def test_batch_allows_own_rows_but_blocks_reentry_between_rows(self):
        with state.batch_session() as token:
            for _ in range(2):
                with self.assertRaises(state.ImporterBusyError):
                    with state.import_session():
                        self.fail('Reentrant event callback entered')
                with self.assertRaises(state.ImporterBusyError):
                    with state.batch_session():
                        self.fail('Concurrent batch entered')
                with state.import_session(token):
                    with self.assertRaises(state.ImporterBusyError):
                        with state.import_session(token):
                            self.fail('Token authorized nested import')
            self.assertTrue(state.is_busy())
        with self.assertRaisesRegex(state.ImporterBusyError, 'nicht mehr aktiv'):
            with state.import_session(token):
                self.fail('Expired batch token entered')

    def test_foreign_thread_cannot_use_batch_token(self):
        errors = []

        def attempt(token):
            try:
                with state.import_session(token):
                    errors.append('Unexpected admission')
            except state.ImporterBusyError as exc:
                errors.append(str(exc))

        with state.batch_session() as token:
            worker = threading.Thread(target=attempt, args=(token,))
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), 'Scene guard waited instead of rejecting')
            self.assertEqual(errors, [state.BUSY_MESSAGE])
            with state.import_session(token):
                pass

    def test_second_thread_cannot_import_during_direct_import(self):
        errors = []

        def attempt():
            try:
                with state.import_session():
                    errors.append('Unexpected admission')
            except state.ImporterBusyError as exc:
                errors.append(str(exc))

        with state.import_session():
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive(), 'Scene guard waited instead of rejecting')
            self.assertEqual(errors, [state.BUSY_MESSAGE])

    def test_failures_release_import_and_batch_reservations(self):
        with self.assertRaisesRegex(ValueError, 'test failure'):
            with state.batch_session() as token:
                with state.import_session(token):
                    raise ValueError('test failure')
        with state.import_session():
            pass
        with state.batch_session():
            pass

    def test_reload_and_import_are_mutually_exclusive(self):
        with state.reload_session():
            self.assertTrue(state.is_busy())
            for session in (state.import_session, state.batch_session, state.reload_session):
                with self.assertRaises(state.ImporterBusyError):
                    with session():
                        self.fail('Operation entered during reload')
        for session in (state.import_session, state.batch_session):
            with session():
                with self.assertRaises(state.ImporterBusyError):
                    with state.reload_session():
                        self.fail('Modules reloaded during active operation')


class StarterTests(unittest.TestCase):
    def setUp(self):
        self.assertFalse(state.is_busy())
        state.set_dialog(None)
        self.core = ModuleType('tencent_importer_core')
        self.core.rt = object()
        self.core._DIALOG = None
        self.core.launch = Mock()

    def tearDown(self):
        state.set_dialog(None)
        self.assertFalse(state.is_busy())

    def run_starter(self):
        with patch.dict(sys.modules, {'tencent_importer_core': self.core}), \
             patch.dict('os.environ', {'TENCENT_FBX_IMPORTER_NO_UI': '0'}), \
             patch.object(importlib, 'reload') as reload:
            runpy.run_path(str(STARTER))
        return reload

    def test_visible_idle_dialog_preserves_its_queue(self):
        current = dialog()
        self.core._DIALOG = current
        reload = self.run_starter()
        reload.assert_not_called()
        self.core.launch.assert_not_called()
        current.close.assert_not_called()
        current.raise_.assert_called_once()
        current.activateWindow.assert_called_once()

    def test_running_dialog_is_reused_during_batch(self):
        current = dialog(running=True, minimized=True)
        state.set_dialog(current)
        with state.batch_session():
            reload = self.run_starter()
        reload.assert_not_called()
        self.core.launch.assert_not_called()
        current.showNormal.assert_called_once()
        current.close.assert_not_called()

    def test_direct_import_without_dialog_prevents_module_reload(self):
        with state.import_session(), patch('builtins.print') as printed:
            reload = self.run_starter()
        reload.assert_not_called()
        self.core.launch.assert_not_called()
        printed.assert_called_once_with(state.BUSY_MESSAGE)

    def test_closed_dialog_allows_reload_but_state_module_is_preserved(self):
        current = dialog(visible=False)
        state.set_dialog(current)
        self.core._DIALOG = current
        reload = self.run_starter()
        modules = [call.args[0].__name__ for call in reload.call_args_list]
        self.assertIn('tencent_importer_core', modules)
        self.assertNotIn('tencent_import_state', modules)
        self.core.launch.assert_called_once()
        self.assertIs(sys.modules['tencent_import_state'], state)

    def test_deleted_qt_wrapper_is_not_reused(self):
        current = dialog()
        current.isVisible.side_effect = RuntimeError('Internal C++ object already deleted')
        state.set_dialog(current)
        self.core._DIALOG = current
        reload = self.run_starter()
        self.assertGreater(reload.call_count, 0)
        self.core.launch.assert_called_once()


if __name__ == '__main__':
    unittest.main()
