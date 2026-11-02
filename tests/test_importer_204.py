"""Fail closed for invalid or ambiguous names before any batch row can start."""
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_importer_core as core
import tencent_import_state as state
import tencent_batch_support as support
from test_importer_202 import make_options


class StrictNameTests(unittest.TestCase):
    def test_comma_and_other_unsupported_characters_are_rejected_not_repaired(self):
        for value in ('Person,1', 'Person.1', 'Person.fbx', 'Person!1', 'Person:1',
                      'Person;1', 'Person?1', 'Person*1', 'Person"1', 'Person<1',
                      'Person>1', 'Person|1', 'Person/1', 'Person\\1', 'Person😀1',
                      'Person\t1', 'Person\n1', '\tPerson1', 'Person1\n',
                      'Person\u00a01', 'Person\u200b1', 'Personé1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.validate_asset_name(value)

    def test_name_boundaries_and_length_are_not_silently_trimmed(self):
        for value in ('', '   ', '_Stuhl', 'Stuhl_', '-Stuhl', 'Stuhl-', '.',
                      '___', 'x' * 41, 'Ä' * 21):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.validate_asset_name(value)
        self.assertEqual(core.validate_asset_name('x' * 40), 'x' * 40)

    def test_supported_german_names_and_simple_spacing_are_normalized(self):
        for raw, expected in [('  Tür groß  ', 'Tuer_gross'), ('Möbel-Ä1', 'Moebel-Ae1'),
                              ('Person  01', 'Person_01'), ('Stuhl_01', 'Stuhl_01'),
                              ('1 Stuhl', '1_Stuhl'), ('STRAẞE', 'STRASSE')]:
            with self.subTest(raw=raw):
                self.assertEqual(core.validate_asset_name(raw), expected)

    def test_exact_user_error_flags_both_duplicates_and_the_comma(self):
        errors = core.asset_name_errors(['nn', 'nn', 'Person,1'])
        self.assertEqual(set(errors), {0, 1, 2})
        for row in (0, 1):
            self.assertIn('doppelt', errors[row].lower())

    def test_duplicates_include_case_spacing_umlauts_and_filename_prefix(self):
        for names in [('Stuhl', 'stuhl'), ('Test Asset', 'Test_Asset'),
                      ('Test  Asset', 'Test_Asset'), ('Tür groß', 'Tuer_gross'),
                      ('Straße', 'STRASSE'), ('Stuhl', 'ai_Stuhl'),
                      ('AI_STUHL', 'stuhl')]:
            with self.subTest(names=names):
                errors = core.asset_name_errors(names)
                self.assertEqual(set(errors), {0, 1})
                self.assertTrue(all('doppelt' in value.lower() for value in errors.values()))

    def test_correcting_or_removing_a_duplicate_clears_its_pair(self):
        self.assertEqual(set(core.asset_name_errors(['nn', 'nn', 'Person,1'])), {0, 1, 2})
        self.assertEqual(set(core.asset_name_errors(['nn', 'Person 2', 'Person,1'])), {2})
        self.assertEqual(core.asset_name_errors(['nn', 'Person 2', 'Person 3']), {})
        self.assertEqual(core.asset_name_errors(['nn']), {})


class BatchNamePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = make_options(self.root, asset_name='Person 1', optimize_enabled=False)
        self.second_source = self.root / 'other.fbx'
        self.second_source.write_bytes(b'other FBX fixture')

    def tearDown(self):
        self.assertFalse(state.is_busy())

    def assert_blocked_without_effects(self, rows, scan_only=False):
        progress, cancelled = Mock(), Mock(return_value=False)
        resolver, confirm = Mock(), Mock()
        with patch.object(core, 'run_import_and_convert') as importer, \
                patch.object(core, 'run_scan_only') as scanner, \
                patch.object(support, 'reserve_asset') as reserve:
            with self.assertRaises(ValueError) as failure:
                core.run_batch(rows, progress=progress, cancelled=cancelled, scan_only=scan_only,
                               overwrite_resolver=resolver, overwrite_confirm=confirm)
        for callback in (importer, scanner, progress, cancelled, resolver, confirm, reserve):
            callback.assert_not_called()
        self.assertFalse(self.options.asset_folder.exists())
        return str(failure.exception)

    def test_user_nn_nn_and_comma_fail_together_before_first_row(self):
        rows = [replace(self.options, asset_name=value) for value in ('nn', 'nn', 'Person,1')]
        error = self.assert_blocked_without_effects(rows)
        self.assertIn('doppelt', error.lower())
        for row in ('1', '2', '3'):
            self.assertIn(row, error)

    def test_invalid_later_row_prevents_an_earlier_valid_import(self):
        for invalid in ('Person,2', '', 'Person.2'):
            with self.subTest(invalid=invalid):
                self.assert_blocked_without_effects([self.options, replace(self.options, asset_name=invalid)])

    def test_invalid_later_row_prevents_all_scan_extraction(self):
        self.assert_blocked_without_effects(
            [self.options, replace(self.options, asset_name='Person,2')], scan_only=True)

    def test_duplicate_names_block_same_and_different_source_paths(self):
        for source in (self.options.fbx_file, self.second_source):
            with self.subTest(source=source):
                self.assert_blocked_without_effects(
                    [self.options, replace(self.options, fbx_file=source, asset_name='Person_1')])

    def test_filename_prefix_alias_is_also_blocked_before_dispatch(self):
        self.assert_blocked_without_effects(
            [self.options, replace(self.options, asset_name='AI_PERSON_1')])

    def test_generator_is_fully_validated_before_dispatch(self):
        self.assert_blocked_without_effects(iter([
            self.options, replace(self.options, asset_name='Person,2')]))

    def test_original_requested_name_cannot_hide_behind_generated_identity(self):
        generated = replace(self.options, asset_name='Person_1_1234_' + 'a' * 32,
                            requested_asset_name='Person,1', asset_id='a' * 32)
        self.assert_blocked_without_effects([generated])

    def test_distinct_valid_names_still_dispatch_in_order_with_callbacks(self):
        rows = [self.options, replace(self.options, asset_name='Person 2')]
        progress, resolver, confirm = Mock(), Mock(), Mock()
        with patch.object(core, 'run_import_and_convert', side_effect=['first', 'second']) as importer:
            result = core.run_batch(iter(rows), progress=progress,
                                    overwrite_resolver=resolver, overwrite_confirm=confirm)
        self.assertEqual([call.args[0] for call in importer.call_args_list], rows)
        self.assertEqual([call.args[1] for call in progress.call_args_list],
                         ['Läuft', 'Fertig', 'Läuft', 'Fertig'])
        self.assertEqual([item['text'] for item in result], ['first', 'second'])
        self.assertTrue(all(call.kwargs['overwrite_resolver'] is resolver
                            and call.kwargs['overwrite_confirm'] is confirm
                            for call in importer.call_args_list))

    def test_distinct_valid_scan_names_still_dispatch(self):
        rows = [self.options, replace(self.options, asset_name='Person 2')]
        with patch.object(core, 'run_scan_only', side_effect=['first', 'second']) as scanner:
            result = core.run_batch(rows, scan_only=True)
        self.assertEqual([call.args[0] for call in scanner.call_args_list], rows)
        self.assertEqual([item['success'] for item in result], [True, True])

    def test_existing_disk_name_is_not_a_duplicate_within_single_row_batch(self):
        self.options.asset_folder.mkdir()
        existing = self.options.asset_folder / 'ai_Person_1_maps'
        existing.mkdir()
        marker = existing / 'existing.txt'
        marker.write_bytes(b'keep until separately confirmed')
        resolver, confirm = Mock(), Mock()
        with patch.object(core, 'run_import_and_convert', return_value='overwrite workflow') as importer:
            result = core.run_batch([self.options], overwrite_resolver=resolver, overwrite_confirm=confirm)
        importer.assert_called_once()
        self.assertIs(importer.call_args.kwargs['overwrite_resolver'], resolver)
        self.assertIs(importer.call_args.kwargs['overwrite_confirm'], confirm)
        self.assertTrue(result[0]['success'])
        self.assertEqual(marker.read_bytes(), b'keep until separately confirmed')


if __name__ == '__main__':
    unittest.main()
