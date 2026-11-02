"""Persistent custom layer names, including shared Max-session updates."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_layer_history as history


class LayerHistoryTests(unittest.TestCase):
    def test_newest_ten_names_survive_a_fresh_read(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config' / 'layers.json'
            for index in range(12):
                history.remember_layer('Layer ' + str(index), path)
            expected = ['Layer ' + str(index) for index in range(11, 1, -1)]
            self.assertEqual(history.recent_layer_names(path), expected)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), expected)

    def test_duplicate_is_moved_to_front_with_latest_spelling(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'layers.json'
            for name in ('Straße Nord', 'Andere Ebene', 'STRASSE NORD'):
                history.remember_layer(name, path)
            self.assertEqual(history.recent_layer_names(path), ['STRASSE NORD', 'Andere Ebene'])

    def test_unicode_and_spaces_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'layers.json'
            name = '  Außenmöbel 日本語 – Süd  '
            history.remember_layer(name, path)
            history.remember_layer('   ', path)
            history.remember_layer(None, path)
            self.assertEqual(history.recent_layer_names(path), [name])
            self.assertIn(name, path.read_text(encoding='utf-8'))

    def test_missing_invalid_or_corrupt_preferences_are_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'layers.json'
            self.assertEqual(history.recent_layer_names(path), [])
            for contents in (b'', b'{broken', b'{}', b'123', b'null', b'\xff\xfe'):
                path.write_bytes(contents)
                self.assertEqual(history.recent_layer_names(path), [])
            path.write_text(json.dumps(['A', '', None, 'a', {}, 'B']), encoding='utf-8')
            self.assertEqual(history.recent_layer_names(path), ['A', 'B'])
            path.write_text('{damaged', encoding='utf-8')
            history.remember_layer('Recovered', path)
            self.assertEqual(history.recent_layer_names(path), ['Recovered'])

    def test_local_appdata_and_home_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {'LOCALAPPDATA': temp}):
                history.remember_layer('Windows User')
                expected = Path(temp) / 'TencentToVray' / 'layers.json'
                self.assertEqual(history.recent_layer_names(expected), ['Windows User'])
            with patch.dict(os.environ, {'LOCALAPPDATA': ''}), patch.object(Path, 'home', return_value=Path(temp)):
                history.remember_layer('Fallback')
                expected = Path(temp) / '.TencentToVray' / 'layers.json'
                self.assertEqual(history.recent_layer_names(expected), ['Fallback'])

    def test_failed_replace_keeps_previous_history_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'layers.json'
            history.remember_layer('Original', path)
            with patch.object(history.os, 'replace', side_effect=OSError('Disk error')):
                with self.assertRaises(OSError):
                    history.remember_layer('Failed', path)
            self.assertEqual(history.recent_layer_names(path), ['Original'])
            self.assertEqual(list(path.parent.glob('.layers_*.json')), [])
            history.remember_layer('Next', path)
            self.assertEqual(history.recent_layer_names(path), ['Next', 'Original'])

    def test_separate_process_writers_preserve_each_others_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'layers.json'
            source = (
                'import sys; from pathlib import Path; '
                'import tencent_layer_history as h; '
                'h.remember_layer(sys.argv[2], Path(sys.argv[1]))'
            )
            processes = [subprocess.Popen([sys.executable, '-c', source, str(path), name],
                                         cwd=str(Path(__file__).resolve().parents[1]),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for name in ('Prozess Eins', 'Prozess Zwei', 'Prozess Drei')]
            for process in processes:
                output, errors = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, (output, errors))
            self.assertCountEqual(history.recent_layer_names(path),
                                  ['Prozess Eins', 'Prozess Zwei', 'Prozess Drei'])


if __name__ == '__main__':
    unittest.main()
