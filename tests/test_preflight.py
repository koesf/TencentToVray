"""Explicit preflight approvals, selective rechecks, and last-moment changes."""
from contextlib import ExitStack, nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_importer_core as core
import tencent_batch_support as support
import tencent_asset_store as store
import tencent_preflight as preflight
from test_importer import fbx, material_fixture, png
from test_importer_202 import RuntimeStub, mesh
import tencent_max_metadata as legacy_metadata


def mesh_fixture(payload=None):
    objects = [('Model', [1, 'Person', 'Mesh'], [])]
    links = []
    if payload is not None:
        objects.extend([
            ('Material', [10, 'Material1'], []),
            ('Texture', [11, 'base_color_texture'], []),
            ('Video', [12, 'texture_pbr.png'], [('Content', [payload], []),
                                              ('RelativeFilename', ['texture_pbr.png'], [])]),
        ])
        links = [('C', ['OP', 11, 10, 'DiffuseColor'], []), ('C', ['OO', 12, 11], [])]
    return fbx([('Objects', [], objects), ('Connections', [], links)])


class ExplicitPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'person.fbx'
        self.source.write_bytes(material_fixture([png((10, 20, 30))]))
        self.other = self.root / 'other.fbx'
        self.other.write_bytes(material_fixture([png((30, 20, 10))]))
        self.target = self.root / 'output'
        self.target.mkdir()
        self.options = core.ImportOptions(self.source, 'Person 1', '260916', self.target,
                                          core.maps_folder_for_target(self.target, 'Person 1'),
                                          optimize_enabled=False, create_layer=False,
                                          texture_downscale_sizes=())
        self.session = preflight.PreflightSession(core)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(legacy_metadata, 'remove_manifest', return_value=False, create=True))
        self.stack.enter_context(patch.object(legacy_metadata, 'write_manifest',
            side_effect=AssertionError('No importer metadata may be written into MAX'), create=True))
        self.plugins = self.stack.enter_context(patch.object(self.session, '_check_plugins'))
        self.stack.enter_context(patch.object(self.session, '_scene_scope', return_value=()))
        self.scan = self.stack.enter_context(patch.object(self.session, '_scan_source', side_effect=
            lambda options: {'text': 'Source checked', 'digest': support.sha256(options.fbx_file)}))

    def checked(self, options=None, **kwargs):
        rows = options if options is not None else [self.options]
        records = self.session.check(rows, **kwargs)
        self.assertEqual(len(records), len(rows))
        self.assertTrue(all(item['success'] for item in records), records)
        self.assertTrue(self.session.ready(rows, offer_overwrite=kwargs.get('offer_overwrite', True)))
        return records

    def existing_asset(self, options=None):
        options = options or self.options
        name = core.validate_asset_name(options.asset_name)
        asset_id = 'a' * 32
        asset = name + '_1907_' + asset_id
        code = core.asset_code(options.prefix, asset)
        folder = core.maps_folder_for_target(options.asset_folder, name) / ('1907_' + asset_id)
        folder.mkdir(parents=True, exist_ok=True)
        max_path = options.asset_folder / (code + '.max')
        map_path = folder / (code + '_basecolor.png')
        max_path.write_bytes(b'old max')
        map_path.write_bytes(png((1, 2, 3)))
        (folder / 'import_report.txt').write_text('Previous import report', encoding='utf-8')
        manifest = {'version': '2.03', 'requested_asset_name': name, 'asset': asset,
                    'asset_id': asset_id, 'asset_code': code, 'import_time_code': '1907',
                    'imported_at': '2026-09-16T19:07:00+02:00', 'max_file': str(max_path),
                    'materials': {'Material1': {'basecolor': {
                        'path': str(map_path), 'filename': map_path.name,
                        'relative_path': map_path.relative_to(options.asset_folder).as_posix(),
                        'sha256': support.sha256(map_path)}}}}
        (folder / 'import_manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return store.discover_assets(core, options)[0]

    def test_no_approval_until_explicit_successful_check(self):
        self.assertFalse(self.session.ready([self.options]))
        with self.assertRaises(preflight.PreflightRequired):
            self.session.assert_current([self.options])
        self.checked()
        self.session.assert_current([self.options])

    def test_exact_user_duplicate_and_comma_never_run_expensive_checks_or_dialogs(self):
        rows = [replace(self.options, asset_name=name) for name in ('nn', 'nn', 'Person,1')]
        resolver, confirmer = Mock(), Mock()
        records = self.session.check(rows, resolver=resolver, confirmer=confirmer)
        self.assertEqual([item['success'] for item in records], [False, False, False])
        self.assertTrue(all(item.get('name_error') for item in records))
        self.scan.assert_not_called()
        self.plugins.assert_not_called()
        resolver.assert_not_called()
        confirmer.assert_not_called()
        self.assertFalse(self.session.ready(rows))

    def test_valid_batch_dispatch_requires_preflight_and_keeps_callback_context(self):
        self.checked()
        with patch.object(core, 'run_import_and_convert', return_value='done') as importer:
            result = core.run_batch([self.options], preflight=self.session)
        self.assertEqual([item['success'] for item in result], [True])
        importer.assert_called_once()

    def test_changed_source_blocks_entire_batch_before_callbacks_or_import(self):
        rows = [self.options, replace(self.options, asset_name='Person 2', fbx_file=self.other)]
        self.checked(rows)
        previous = self.other.stat()
        old = self.other.read_bytes()
        self.other.write_bytes(old[:-1] + bytes([old[-1] ^ 1]))
        os.utime(self.other, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        progress = Mock()
        with patch.object(core, 'run_import_and_convert') as importer:
            with self.assertRaises(preflight.PreflightRequired):
                core.run_batch(rows, progress=progress, preflight=self.session)
        importer.assert_not_called()
        progress.assert_not_called()

    def test_changed_settings_name_target_or_source_require_recheck(self):
        self.checked()
        for changes in ({'asset_name': 'Person 2'}, {'fbx_file': self.other},
                        {'asset_folder': self.root / 'elsewhere'}, {'import_textures': False},
                        {'optimize_enabled': True}, {'optimize_percent': 20},
                        {'save_max_file': False}, {'group_imported_objects': True},
                        {'create_layer': True, 'layer_custom_name': 'Test Layer'},
                        {'texture_downscale_sizes': (512,)}):
            with self.subTest(changes=changes):
                modified = replace(self.options, **changes)
                self.assertFalse(self.session.ready([modified]))
                with self.assertRaises(preflight.PreflightRequired):
                    self.session.assert_current([modified])
        self.assertFalse(self.session.ready([self.options], offer_overwrite=False))

    def test_late_new_asset_conflict_blocks_before_first_import(self):
        self.checked()
        self.existing_asset()
        with patch.object(core, 'run_import_and_convert') as importer:
            with self.assertRaises(preflight.PreflightRequired):
                core.run_batch([self.options], preflight=self.session)
        importer.assert_not_called()

    def test_existing_asset_requires_decision_during_preflight(self):
        self.existing_asset()
        resolver = Mock(return_value=None)
        confirmer = Mock()
        self.checked(resolver=resolver, confirmer=confirmer)
        resolver.assert_called_once()
        confirmer.assert_not_called()
        self.assertIsNone(self.session.resolve(self.options, store.discover_assets(core, self.options)))

    def test_existing_asset_filename_prefix_alias_still_requires_decision(self):
        candidate = self.existing_asset()
        alias = replace(self.options, asset_name='AI_Person_1')
        resolver = Mock(return_value=None)
        self.checked([alias], resolver=resolver)
        resolver.assert_called_once()
        self.assertEqual(resolver.call_args.args[1][0]['manifest']['asset_id'],
                         candidate['manifest']['asset_id'])

    def test_legacy_custom_map_suffix_blocks_replacement_before_confirmation_but_allows_new(self):
        candidate = self.existing_asset()
        manifest = candidate['manifest']
        item = manifest['materials']['Material1']['basecolor']
        original = Path(item['path'])
        custom = original.with_name(manifest['asset_code'] + '_customImage.png')
        original.rename(custom)
        item.update(path=str(custom), filename=custom.name,
                    relative_path=custom.relative_to(self.target).as_posix())
        candidate['manifest_path'].write_text(json.dumps(manifest), encoding='utf-8')
        resolver = Mock(side_effect=lambda options, items: items[0])
        confirmer = Mock(return_value=True)
        with patch.object(core, 'import_fbx_and_get_nodes') as importer:
            records = self.session.check([self.options], resolver=resolver, confirmer=confirmer)
        self.assertFalse(records[0]['success'], records)
        self.assertIn('Bitte als Neu importieren', records[0]['text'])
        resolver.assert_called_once()
        confirmer.assert_not_called()
        importer.assert_not_called()
        self.assertTrue(candidate['manifest_path'].is_file())
        self.assertEqual(custom.read_bytes(), png((1, 2, 3)))
        self.checked(resolver=lambda options, items: None)

    def test_legacy_standard_map_names_allow_confirmed_replacement(self):
        self.existing_asset()
        confirmer = Mock(return_value=True)
        self.checked(resolver=lambda options, items: items[0], confirmer=confirmer)
        confirmer.assert_called_once()

    def test_replacement_approval_occurs_during_preflight_and_paths_are_concrete(self):
        candidate = self.existing_asset()
        resolver = Mock(side_effect=lambda options, candidates: candidates[0])
        confirmer = Mock(return_value=True)
        self.checked(resolver=resolver, confirmer=confirmer)
        confirmer.assert_called_once()
        plan = confirmer.call_args.args[2]
        self.assertEqual(Path(plan['max_file']), candidate['max_file'])
        self.assertEqual(Path(plan['texture_folder']), candidate['texture_folder'])
        self.assertIn(str(candidate['max_file']), [str(path) for path in plan['files']])
        selected = self.session.resolve(self.options, store.discover_assets(core, self.options))
        self.assertEqual(selected['manifest']['asset_id'], candidate['manifest']['asset_id'])
        self.assertTrue(self.session.confirm(self.options, selected, plan))
        confirmer.assert_called_once()

    def test_declined_replacement_is_not_ready_and_files_stay_intact(self):
        candidate = self.existing_asset()
        before = {path: path.read_bytes() for path in candidate['managed_paths']}
        records = self.session.check([self.options], resolver=lambda options, items: items[0],
                                     confirmer=lambda *args: False)
        self.assertFalse(records[0]['success'])
        self.assertFalse(self.session.ready([self.options]))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_changed_existing_asset_after_approval_blocks(self):
        candidate = self.existing_asset()
        self.checked(resolver=lambda options, items: items[0], confirmer=lambda *args: True)
        path = candidate['max_file']
        previous = path.stat()
        path.write_bytes(b'NEW MAX')
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        with self.assertRaises(preflight.PreflightRequired):
            self.session.assert_current([self.options])

    def test_orphan_target_folder_is_blocked_instead_of_silently_treated_as_new(self):
        folder = core.maps_folder_for_target(self.target, self.options.asset_name)
        folder.mkdir()
        (folder / 'foreign.png').write_bytes(b'keep')
        records = self.session.check([self.options])
        self.assertFalse(records[0]['success'], records)
        self.assertFalse(self.session.ready([self.options]))
        self.assertEqual((folder / 'foreign.png').read_bytes(), b'keep')

    def test_corrupt_manifest_target_is_blocked(self):
        candidate = self.existing_asset()
        candidate['manifest_path'].write_text('{broken', encoding='utf-8')
        resolver = Mock()
        records = self.session.check([self.options], resolver=resolver)
        self.assertFalse(records[0]['success'], records)
        resolver.assert_not_called()

    def test_invalidating_approval_requires_explicit_preflight_again(self):
        self.checked()
        self.session.invalidate()
        self.assertFalse(self.session.ready([self.options]))
        with self.assertRaises(preflight.PreflightRequired):
            self.session.assert_current([self.options])
        self.checked()

    def test_successful_sources_are_cached_and_only_changed_source_is_rescanned(self):
        rows = [self.options, replace(self.options, asset_name='Person 2', fbx_file=self.other)]
        self.checked(rows)
        self.assertEqual(self.scan.call_count, 2)
        records = self.checked(rows)
        self.assertTrue(all(record['reused'] for record in records))
        self.assertEqual(self.scan.call_count, 2)
        renamed = [self.options, replace(rows[1], asset_name='Person neu')]
        self.checked(renamed)
        self.assertEqual(self.scan.call_count, 2)
        self.other.write_bytes(self.other.read_bytes() + b'changed')
        records = self.checked(renamed)
        self.assertTrue(records[0]['reused'])
        self.assertFalse(records[1]['reused'])
        self.assertEqual(self.scan.call_count, 3)

    def test_fixing_failed_row_reuses_successful_row_but_retries_failed_source(self):
        rows = [self.options, replace(self.options, asset_name='Person 2', fbx_file=self.other)]
        self.scan.side_effect = [dict(text='ok', digest=support.sha256(self.source)),
                                 ValueError('Broken image'),
                                 dict(text='fixed', digest=support.sha256(self.other))]
        first = self.session.check(rows)
        self.assertEqual([item['success'] for item in first], [True, False])
        self.assertFalse(self.session.ready(rows))
        second = self.checked(rows)
        self.assertEqual([item['reused'] for item in second], [True, False])
        self.assertEqual(self.scan.call_count, 3)

    def test_successful_existing_target_decision_is_not_asked_again_when_unchanged(self):
        self.existing_asset()
        resolver = Mock(side_effect=lambda options, candidates: candidates[0])
        confirmer = Mock(return_value=True)
        self.checked(resolver=resolver, confirmer=confirmer)
        self.checked(resolver=resolver, confirmer=confirmer)
        resolver.assert_called_once()
        confirmer.assert_called_once()

    def test_failure_during_import_stops_remaining_approved_rows_and_revokes_approval(self):
        rows = [self.options, replace(self.options, asset_name='Person 2', fbx_file=self.other)]
        self.checked(rows)
        with patch.object(core, 'run_import_and_convert', side_effect=RuntimeError('runtime failure')) as importer:
            result = core.run_batch(rows, preflight=self.session)
        importer.assert_called_once()
        self.assertEqual([item['success'] for item in result], [False])
        self.assertFalse(self.session.ready(rows))

    def test_late_compact_max_orphan_requires_preflight_without_import(self):
        self.checked()
        orphan = self.target / 'ai_Person_1_1907_ABCD2345_260916.max'
        orphan.write_bytes(b'foreign MAX')
        with patch.object(core, 'run_import_and_convert') as importer:
            with self.assertRaises(preflight.PreflightRequired):
                core.run_batch([self.options], preflight=self.session)
        importer.assert_not_called()
        self.assertEqual(orphan.read_bytes(), b'foreign MAX')

    def recovery_plan(self, name='Person_1', payload=None):
        folder = self.target / '.tencent_stage_recovery_fixture'
        folder.mkdir(exist_ok=True)
        # Deliberately an old workstation path: identity matching must survive
        # relocating the complete output tree to another drive/share.
        data = {'status': 'prepared', 'targets': [{
            'path': 'X:/EarlierShare/ai_' + name + '_1907_ABCD2345_260916.max',
            'backup': 'X:/EarlierShare/.tencent_stage_old/_backups/0',
            'operation': 'replace', 'state': 'pending'}]}
        (folder / '_recovery.json').write_text(json.dumps(data) if payload is None else payload, encoding='utf-8')
        return folder

    def test_matching_recovery_blocks_new_and_replacement_even_when_overwrite_is_off(self):
        self.existing_asset()
        folder = self.recovery_plan()
        before = {path: path.read_bytes() for path in folder.iterdir()}
        for offer in (True, False):
            with self.subTest(offer_overwrite=offer):
                resolver, confirmer = Mock(return_value=None), Mock(return_value=True)
                records = self.session.check([self.options], resolver=resolver, confirmer=confirmer,
                                             offer_overwrite=offer)
                self.assertFalse(records[0]['success'], records)
                self.assertIn(str(folder), records[0]['text'])
                self.assertIn('Wiederherstellung', records[0]['text'])
                resolver.assert_not_called()
                confirmer.assert_not_called()
                self.assertFalse(self.session.ready([self.options], offer_overwrite=offer))
        self.assertEqual({path: path.read_bytes() for path in folder.iterdir()}, before)

    def test_valid_unrelated_recovery_does_not_block_another_asset_name(self):
        self.recovery_plan('Person_10')
        self.checked()
        self.session.assert_current([self.options])

    def test_preparing_transaction_with_identity_does_not_block_another_asset(self):
        max_file = self.target / 'ai_Person_10_1907_ABCD2345_260916.max'
        folder = self.target / 'ai_Person_10_maps' / '1907_ABCD2345'
        with store.AssetTransaction(self.target, folder, max_file) as tx:
            self.assertEqual((tx.stage_dir / '_asset_identity.txt').read_text().strip(), max_file.name)
            self.assertFalse((tx.stage_dir / '_recovery.json').exists())
            self.assertEqual(list(self.target.glob('.tencent_prepare_*')), [])
            self.checked()
            self.session.assert_current([self.options])

    def test_preparing_transaction_with_matching_identity_blocks_new_import(self):
        max_file = self.target / 'ai_Person_1_1907_ABCD2345_260916.max'
        folder = self.target / 'ai_Person_1_maps' / '1907_ABCD2345'
        with store.AssetTransaction(self.target, folder, max_file) as tx:
            records = self.session.check([self.options], offer_overwrite=False)
            self.assertFalse(records[0]['success'], records)
            self.assertIn(str(tx.stage_dir), records[0]['text'])
            self.assertIn('Import läuft', records[0]['text'])

    def test_unknown_or_malformed_recovery_blocks_entire_target_conservatively(self):
        for payload in ('{broken', '[]', '{"targets": []}',
                        '{"targets": [{"path": "relative.max"}]}',
                        '{"targets": [{"path": "X:/old/foreign.max"}]}',
                        '{"targets": [{"path": "X:/old/map.png"}]}',
                        '{"targets": [{"path": "X:/old/../foreign.max"}]}'):
            with self.subTest(payload=payload):
                folder = self.recovery_plan('OtherAsset', payload=payload)
                records = self.session.check([self.options], offer_overwrite=False)
                self.assertFalse(records[0]['success'], records)
                self.assertIn(str(folder), records[0]['text'])
                self.assertIn('nicht sicher zuordenbar', records[0]['text'])

    def test_missing_and_oversized_recovery_plan_fail_closed(self):
        folder = self.recovery_plan('Unrelated')
        journal = folder / '_recovery.json'
        journal.unlink()
        self.assertFalse(self.session.check([self.options])[0]['success'])
        journal.write_bytes(b' ' * (8 * 1024 * 1024 + 1))
        self.assertFalse(self.session.check([self.options])[0]['success'])

    def test_recovery_appearing_after_approval_blocks_before_native_import(self):
        self.checked()
        self.recovery_plan()
        with patch.object(core, 'run_import_and_convert') as importer:
            with self.assertRaises(preflight.PreflightRequired):
                core.run_batch([self.options], preflight=self.session)
        importer.assert_not_called()

    def test_published_progress_does_not_bypass_remaining_recovery_folder(self):
        folder = self.recovery_plan()
        (folder / '_progress.jsonl').write_text('{"status": "published"}\n', encoding='utf-8')
        self.assertFalse(self.session.check([self.options])[0]['success'])

    def test_missing_embedded_maps_block_until_textures_are_switched_off(self):
        self.source.write_bytes(mesh_fixture())
        original_scan = preflight.PreflightSession._scan_source.__get__(self.session)
        with patch.object(self.session, '_scan_source', wraps=original_scan) as real_scan:
            records = self.session.check([self.options])
            self.assertFalse(records[0]['success'], records)
            self.assertIn('Texturen ausschalten', records[0]['text'])
            mesh_only = replace(self.options, import_textures=False)
            self.checked([mesh_only])
            self.assertEqual(real_scan.call_count, 2)

    def test_embedded_images_are_decoded_before_import_and_bad_image_blocks(self):
        self.source.write_bytes(mesh_fixture(png((12, 34, 56))))
        original_scan = preflight.PreflightSession._scan_source.__get__(self.session)
        with patch.object(self.session, '_scan_source', wraps=original_scan):
            self.checked()
            self.source.write_bytes(mesh_fixture(core.PNG_SIGNATURE + b'not an image'))
            records = self.session.check([self.options])
            self.assertFalse(records[0]['success'], records)
            self.assertFalse(self.session.ready([self.options]))

    def test_broken_fbx_and_nonmesh_fbx_fail_before_import(self):
        original_scan = preflight.PreflightSession._scan_source.__get__(self.session)
        with patch.object(self.session, '_scan_source', wraps=original_scan):
            for content in (b'not an FBX', material_fixture([png((1, 2, 3))])):
                with self.subTest(content=content[:20]):
                    self.source.write_bytes(content)
                    records = self.session.check([self.options])
                    self.assertFalse(records[0]['success'], records)
                    self.assertFalse(self.session.ready([self.options]))

    def test_changed_private_source_copy_blocks_before_reservation_extraction_or_geometry(self):
        self.checked()
        runtime = RuntimeStub()
        copy_file = support.shutil.copyfile

        def corrupt_copy(source, target, *args, **kwargs):
            result = copy_file(source, target, *args, **kwargs)
            Path(target).write_bytes(b'changed during snapshot')
            return result

        with patch.object(core, 'rt', runtime), \
                patch.object(support.shutil, 'copyfile', side_effect=corrupt_copy), \
                patch.object(support, 'reserve_asset') as reserve, \
                patch.object(support, 'extract_material_maps') as extract, \
                patch.object(core, 'import_fbx_and_get_nodes') as importer:
            with self.assertRaises(preflight.PreflightRequired):
                core.run_batch([self.options], preflight=self.session)
        reserve.assert_not_called()
        extract.assert_not_called()
        importer.assert_not_called()
        self.assertEqual(list(self.target.iterdir()), [])

    def test_unowned_image_in_existing_asset_folder_requires_clarification(self):
        candidate = self.existing_asset()
        stray = candidate['texture_folder'] / 'unowned.png'
        stray.write_bytes(png((3, 2, 1)))
        resolver = Mock()
        records = self.session.check([self.options], resolver=resolver)
        self.assertFalse(records[0]['success'], records)
        resolver.assert_not_called()
        self.assertEqual(stray.read_bytes(), png((3, 2, 1)))

    def test_ready_is_cheap_and_never_reads_files_scans_or_prompts(self):
        self.checked()
        with patch.object(Path, 'stat', side_effect=AssertionError('ready must not stat files')), \
                patch.object(support, 'sha256', side_effect=AssertionError('ready must not hash files')), \
                patch.object(self.session, '_scan_source', side_effect=AssertionError('ready must not scan')):
            self.assertTrue(self.session.ready([self.options]))

    def test_callback_cannot_begin_nested_import_or_preflight(self):
        nested = []
        def progress(*unused):
            import tencent_import_state as state
            for operation in (lambda: self.session.check([self.options]),
                              lambda: core.run_batch([self.options])):
                with self.assertRaises(state.ImporterBusyError):
                    operation()
                nested.append(True)
        self.checked(progress=progress)
        self.assertEqual(len(nested), 2)

    def test_locked_existing_asset_cannot_be_approved_for_replacement(self):
        candidate = self.existing_asset()
        lock = candidate['texture_folder'] / '.tencent_asset.lock'
        with store.AssetLock(lock):
            records = self.session.check([self.options], resolver=lambda options, items: items[0],
                                         confirmer=lambda *unused: True)
        self.assertFalse(records[0]['success'], records)
        self.assertFalse(self.session.ready([self.options]))

    def test_locked_existing_asset_does_not_block_explicit_new_copy(self):
        candidate = self.existing_asset()
        lock = candidate['texture_folder'] / '.tencent_asset.lock'
        with store.AssetLock(lock):
            self.checked(resolver=lambda options, items: None)
            self.session.assert_current([self.options])

    def test_filename_single_material_replacement_blocks_multi_source_before_import(self):
        candidate = self.existing_asset()
        candidate['manifest_path'].unlink()
        (candidate['texture_folder'] / 'import_report.txt').unlink()
        candidate = store.discover_assets(core, self.options)[0]
        self.assertEqual(candidate['metadata_storage'], 'filename')
        self.scan.side_effect = lambda options: {
            'text': 'Two materials checked', 'digest': support.sha256(options.fbx_file), 'material_count': 2}
        confirmer = Mock(return_value=True)
        with patch.object(core, 'import_fbx_and_get_nodes') as importer:
            records = self.session.check([self.options], resolver=lambda options, items: items[0],
                                         confirmer=confirmer)
            self.assertFalse(records[0]['success'], records)
            self.assertIn('Materialaufteilung', records[0]['text'])
            self.assertFalse(self.session.ready([self.options]))
        importer.assert_not_called()
        confirmer.assert_not_called()
        # The same checked source can deliberately become a separate asset.
        self.checked(resolver=lambda options, items: None)

    def test_filename_material_mismatch_does_not_block_mesh_only_replacement(self):
        candidate = self.existing_asset()
        candidate['manifest_path'].unlink()
        (candidate['texture_folder'] / 'import_report.txt').unlink()
        self.checked([replace(self.options, import_textures=False)],
                     resolver=lambda options, items: items[0], confirmer=lambda *args: True)

    def test_filename_missing_material_index_blocks_before_replacement_confirmation(self):
        candidate = self.existing_asset()
        old_map = Path(candidate['manifest']['materials']['Material1']['basecolor']['path'])
        numbered = core.asset_code(candidate['prefix'], candidate['manifest']['asset'] + '_mat02')
        old_map.rename(old_map.with_name(numbered + '_basecolor.png'))
        candidate['manifest_path'].unlink()
        (candidate['texture_folder'] / 'import_report.txt').unlink()
        self.scan.side_effect = lambda options: {
            'text': 'One material checked', 'digest': support.sha256(options.fbx_file), 'material_count': 1}
        confirmer = Mock(return_value=True)
        records = self.session.check([self.options], resolver=lambda options, items: items[0], confirmer=confirmer)
        self.assertFalse(records[0]['success'], records)
        self.assertIn('Materialaufteilung', records[0]['text'])
        confirmer.assert_not_called()

    def test_geometry_uses_preflight_snapshot_if_original_changes_mid_import(self):
        options = replace(self.options, import_textures=False)
        original_bytes = self.source.read_bytes()
        original_hash = support.sha256(self.source)
        self.checked([options])
        runtime = RuntimeStub()
        imported_paths = []

        def import_nodes(path, report):
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(support.sha256(path), original_hash)
            self.assertNotEqual(path, self.source)
            imported_paths.append(path)
            self.source.write_bytes(b'original FBX replaced while importing snapshot')
            node = mesh('Snapshot geometry')
            runtime.objects.append(node)
            return [node]

        with patch.object(core, 'rt', runtime), \
                patch.object(core, 'pymxs', SimpleNamespace(redraw=lambda enabled: nullcontext())), \
                patch.object(core, 'import_fbx_and_get_nodes', side_effect=import_nodes), \
                patch.object(core, 'rename_nodes'), patch.object(core, 'create_layer_for_nodes'):
            records = core.run_batch([options], preflight=self.session)
        self.assertEqual([item['success'] for item in records], [True], records)
        self.assertEqual(len(imported_paths), 1)
        saved_max = next(self.target.glob('*.max'))
        self.assertEqual(saved_max.read_bytes(), b'asset-only MAX fixture')
        runtime.setUserProp.assert_not_called()
        self.assertEqual(list(self.target.rglob('*.json')), [])
        self.assertNotEqual(original_hash, support.sha256(self.source))
if __name__ == '__main__':
    unittest.main()
