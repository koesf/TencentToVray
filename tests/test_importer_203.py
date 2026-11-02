"""Importer entry-point and replacement regressions without a Max runtime."""
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_importer_core as core
import tencent_batch_support as support
import tencent_import_state as state
import tencent_asset_store as store
from test_importer_202 import RuntimeStub, make_options, mesh
import tencent_max_metadata as legacy_metadata


class BatchGuardTests(unittest.TestCase):
    def tearDown(self):
        self.assertFalse(state.is_busy())

    def test_progress_callbacks_cannot_start_another_batch(self):
        attempts = []

        def progress(*args):
            with self.assertRaises(state.ImporterBusyError):
                core.run_batch(['nested'], scan_only=True)
            attempts.append(args[1])

        with patch.object(core, 'run_scan_only', return_value='Geprüft'):
            results = core.run_batch([SimpleNamespace(asset_name='a'), SimpleNamespace(asset_name='b')],
                                     progress=progress, scan_only=True)
        self.assertEqual(attempts, ['Läuft', 'Fertig', 'Läuft', 'Fertig'])
        self.assertEqual([row['success'] for row in results], [True, True])

    def test_cancelled_replacement_stops_the_remaining_rows(self):
        with patch.object(core, 'run_import_and_convert', side_effect=core.ImportCancelled('Abgebrochen')) as run:
            progress = Mock()
            results = core.run_batch([SimpleNamespace(asset_name='first'), SimpleNamespace(asset_name='second')],
                                     progress=progress)
        self.assertEqual(results, [])
        run.assert_called_once()
        self.assertEqual(progress.call_args.args[1], 'Gestoppt')

    def test_failed_progress_callback_releases_batch_guard(self):
        with self.assertRaisesRegex(ValueError, 'callback failure'):
            core.run_batch([SimpleNamespace(asset_name='first')], progress=Mock(side_effect=ValueError('callback failure')))
        self.assertFalse(state.is_busy())


class DialogReuseTests(unittest.TestCase):
    def setUp(self):
        state.set_dialog(None)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(state.set_dialog, None)
        self.stack.enter_context(patch.object(core, '_DIALOG', None))
        self.stack.enter_context(patch.object(core, 'QtWidgets', SimpleNamespace(QApplication=Mock())))

    def test_direct_show_preserves_visible_dialog_during_batch(self):
        dialog = Mock()
        dialog.isVisible.return_value = True
        dialog.isMinimized.return_value = False
        dialog._running = True
        state.set_dialog(dialog)
        with state.batch_session(), patch.object(core, 'ImporterDialog') as factory:
            self.assertIs(core.show(), dialog)
        factory.assert_not_called()
        dialog.close.assert_not_called()
        dialog.raise_.assert_called_once()

    def test_direct_show_during_headless_import_does_not_open_dialog(self):
        with state.import_session(), patch.object(core, 'ImporterDialog') as factory:
            with self.assertRaises(state.ImporterBusyError):
                core.show()
        factory.assert_not_called()


class SceneRuntime(RuntimeStub):
    """Model node deletion, properties, selection and Max's swallowing undo."""
    def __init__(self):
        super().__init__()
        self.properties = {}
        self.save_count = 0
        self.setUserProp = Mock(side_effect=self._set_user_prop)
        self.getUserProp = Mock(side_effect=lambda node, key: self.properties.get(id(node), {}).get(key))
        self.doesUserPropExist = Mock(side_effect=lambda node, key: key in self.properties.get(id(node), {}))
        self.deleteUserProp = Mock(side_effect=lambda node, key: self.properties.get(id(node), {}).pop(key))
        self.select = Mock(side_effect=lambda nodes: setattr(self, 'selection', list(nodes)))
        self.delete = Mock(side_effect=self._delete)
        self.isGroupHead = Mock(return_value=False)
        self.delete_failure = False

    def _set_user_prop(self, node, key, value):
        self.properties.setdefault(id(node), {})[key] = value

    def _delete(self, nodes):
        keys = {id(node) for node in nodes}
        self.objects[:] = [node for node in self.objects if id(node) not in keys]
        if self.delete_failure:
            self.delete_failure = False
            raise RuntimeError('Injected deletion failure')

    def isValidNode(self, node):
        return any(item is node for item in self.objects)

    def saveNodes(self, nodes, target, **kwargs):
        super().saveNodes(nodes, target, **kwargs)
        self.save_count += 1
        Path(target).write_bytes(('MAX revision ' + str(self.save_count)).encode('utf-8'))
        return True

    @contextmanager
    def undo(self, *args):
        before = list(self.objects)
        try:
            yield
        except Exception:
            # pymxs undoes the changes and suppresses the inner exception.
            self.objects[:] = before


class ReplacementPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = make_options(self.root, optimize_enabled=False, import_textures=True)
        self.runtime = SceneRuntime()
        self.unrelated = mesh('Unrelated object')
        self.runtime.objects.append(self.unrelated)
        self.runtime.selection = [self.unrelated]
        self.source_map = self.root / 'source_map.png'
        self.source_map.write_bytes(b'first embedded texture')
        self.imported = []

        def import_nodes(*args):
            node = mesh('Imported ' + str(len(self.imported)))
            self.imported.append(node)
            self.runtime.objects.append(node)
            return [node]

        def rename_nodes(nodes, options, report):
            for index, node in enumerate(nodes):
                node.name = core.asset_code(options.prefix, options.asset_name) + '_GEO_' + str(index)

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(legacy_metadata, 'write_manifest',
            side_effect=AssertionError('No importer metadata may be written into MAX'), create=True))
        self.stack.enter_context(patch.object(legacy_metadata, 'remove_manifest', return_value=False, create=True))
        patches = {
            'rt': self.runtime,
            'pymxs': SimpleNamespace(redraw=lambda enabled: nullcontext(), undo=self.runtime.undo),
            'import_fbx_and_get_nodes': Mock(side_effect=import_nodes),
            'rename_nodes': Mock(side_effect=rename_nodes),
            'create_layer_for_nodes': Mock(),
            'build_vray_material': Mock(side_effect=lambda maps, options, report:
                SimpleNamespace(name='Converted', maps=maps)),
        }
        for name, value in patches.items():
            self.stack.enter_context(patch.object(core, name, value))
        self.stack.enter_context(patch.object(support, 'extract_material_maps', return_value={
            'Material': {'basecolor': core.MapHit('basecolor', self.source_map, 1000)},
        }))
        self.stack.enter_context(patch.object(support, 'verify_material'))

    def tearDown(self):
        self.assertFalse(state.is_busy())
        self.runtime.setUserProp.assert_not_called()

    def first_import(self):
        core.run_import_and_convert(self.options)
        candidates = store.discover_assets(core, self.options)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        snapshot = {path: path.read_bytes() for path in candidate['managed_paths'] if path.exists()}
        return candidate, snapshot, self.imported[-1]

    def assert_original_files(self, snapshot):
        for path, contents in snapshot.items():
            self.assertEqual(path.read_bytes(), contents, str(path))

    def test_selecting_new_version_keeps_both_assets_and_unique_map_paths(self):
        candidate, original, old_node = self.first_import()
        resolver = Mock(return_value=None)
        confirm = Mock(side_effect=AssertionError('New version must not confirm replacement'))
        core.run_import_and_convert(self.options, overwrite_resolver=resolver, overwrite_confirm=confirm)
        resolver.assert_called_once()
        confirm.assert_not_called()
        candidates = store.discover_assets(core, self.options)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len({item['manifest']['asset_id'] for item in candidates}), 2)
        self.assertEqual(len({item['manifest']['materials']['Material']['basecolor']['path'] for item in candidates}), 2)
        self.assert_original_files(original)
        self.assertTrue(self.runtime.isValidNode(old_node))
        self.assertEqual(len(self.runtime.objects), 3)

    def test_declined_confirmation_happens_after_staging_and_preserves_scene_and_files(self):
        candidate, original, old_node = self.first_import()
        selected_before = list(self.runtime.selection)
        self.source_map.write_bytes(b'replacement texture')

        def deny(options, selected, plan):
            self.assert_original_files(original)
            self.assertEqual(self.runtime.save_count, 2)
            self.assertEqual(len(list(self.options.asset_folder.glob('.tencent_stage_*/max/*.max'))), 1)
            self.assertEqual(plan['max_file'], str(candidate['max_file']))
            self.assertEqual(plan['scene_nodes'], [old_node.name])
            self.assertTrue(self.runtime.isValidNode(old_node))
            return False

        with self.assertRaises(core.ImportCancelled):
            core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                        overwrite_confirm=deny)
        self.assert_original_files(original)
        self.assertEqual(self.runtime.objects, [self.unrelated, old_node])
        self.assertEqual(self.runtime.selection, selected_before)
        self.assertEqual(list(self.options.asset_folder.glob('.tencent_stage_*')), [])

    def test_confirmed_replacement_preserves_identity_and_paths_and_removes_old_nodes(self):
        candidate, original, old_node = self.first_import()
        self.source_map.write_bytes(b'replacement texture')
        confirmed = []

        def approve(options, selected, plan):
            self.assert_original_files(original)
            self.assertEqual(self.runtime.save_count, 2)
            with self.assertRaises(state.ImporterBusyError):
                core.run_import_and_convert(self.options)
            with self.assertRaises(state.ImporterBusyError):
                core.run_batch([self.options])
            confirmed.append(plan)
            return True

        core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                    overwrite_confirm=approve)
        self.assertEqual(len(confirmed), 1)
        current = store.discover_assets(core, self.options)
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['manifest']['asset_id'], candidate['manifest']['asset_id'])
        self.assertEqual(current[0]['max_file'], candidate['max_file'])
        self.assertEqual(current[0]['texture_folder'], candidate['texture_folder'])
        record = current[0]['manifest']['materials']['Material']['basecolor']
        self.assertEqual(record['path'], candidate['manifest']['materials']['Material']['basecolor']['path'])
        self.assertEqual(Path(record['path']).read_bytes(), b'replacement texture')
        self.assertEqual(candidate['max_file'].read_bytes(), b'MAX revision 2')
        self.assertFalse(self.runtime.isValidNode(old_node))
        self.assertEqual(self.runtime.objects, [self.unrelated, self.imported[-1]])
        self.assertEqual(self.runtime.selection, [self.imported[-1]])
        self.assertEqual(list(self.options.asset_folder.glob('.tencent_stage_*')), [])

    def test_scene_deletion_failure_rolls_back_files_and_restores_old_scene(self):
        candidate, original, old_node = self.first_import()
        self.source_map.write_bytes(b'replacement texture')
        self.runtime.delete_failure = True
        with self.assertRaisesRegex(RuntimeError, 'wiederhergestellt'):
            core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                        overwrite_confirm=lambda *args: True)
        self.assert_original_files(original)
        self.assertEqual(self.runtime.objects, [self.unrelated, old_node])
        self.assertEqual(self.runtime.selection, [old_node])

    def test_staged_max_cleanup_failure_aborts_before_any_publication(self):
        candidate, original, old_node = self.first_import()
        with patch.object(legacy_metadata, 'remove_manifest', side_effect=legacy_metadata.MetadataError('Injected storage failure')):
            with self.assertRaisesRegex(RuntimeError, 'Injected storage failure'):
                core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                            overwrite_confirm=lambda *args: True)
        self.assert_original_files(original)
        self.assertEqual(self.runtime.objects, [self.unrelated, old_node])
        self.assertEqual(list(self.options.asset_folder.glob('.tencent_stage_*')), [])
        self.assertFalse((candidate['texture_folder'] / '.tencent_asset.lock').exists())

    def test_existing_candidate_requires_a_separate_final_confirmation(self):
        candidate, original, old_node = self.first_import()
        with self.assertRaisesRegex(ValueError, 'ausdrückliche Bestätigung'):
            core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0])
        self.assert_original_files(original)
        self.assertEqual(self.runtime.save_count, 1)
        self.assertEqual(self.runtime.objects, [self.unrelated, old_node])

    def test_import_from_batch_progress_is_rejected_before_scene_changes(self):
        attempts = []

        def progress(*args):
            with self.assertRaises(state.ImporterBusyError):
                core.run_import_and_convert(self.options)
            attempts.append(args[1])

        result = core.run_batch([self.options], progress=progress)
        self.assertEqual([row['success'] for row in result], [True])
        self.assertEqual(attempts, ['Läuft', 'Fertig'])
        self.assertEqual(len(self.imported), 1)

    def test_replacement_updates_all_requested_downscale_variants(self):
        self.options = replace(self.options, texture_downscale_sizes=(1024, 512))

        def resize(source, target, size, kind, report):
            target.write_bytes(source.read_bytes() + str(size).encode('ascii'))

        with patch.object(core, 'usable_downscale_sizes', return_value=(1024, 512)), \
                patch.object(core, 'resize_texture_file', side_effect=resize):
            candidate, original, old_node = self.first_import()
            self.source_map.write_bytes(b'updated texture')
            core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                        overwrite_confirm=lambda *args: True)
        variants = list(candidate['texture_folder'].glob('*.png'))
        self.assertEqual(len(variants), 2)
        for variant in variants:
            self.assertTrue(variant.read_bytes().startswith(b'updated texture'))

    def test_texture_off_then_on_keeps_original_relink_path(self):
        candidate, original, old_node = self.first_import()
        original_record = candidate['manifest']['materials']['Material']['basecolor']
        self.options = replace(self.options, import_textures=False)
        core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                    overwrite_confirm=lambda *args: True)
        middle = store.discover_assets(core, self.options)[0]
        self.assertEqual(middle['manifest']['materials']['Material']['basecolor']['path'], original_record['path'])
        self.assertEqual(Path(original_record['path']).read_bytes(), b'first embedded texture')
        self.assertIsNone(self.imported[-1].material)
        self.options = replace(self.options, import_textures=True)
        self.source_map.write_bytes(b'restored changed texture')
        core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                    overwrite_confirm=lambda *args: True)
        final = store.discover_assets(core, self.options)[0]
        final_record = final['manifest']['materials']['Material']['basecolor']
        self.assertEqual(final_record['path'], original_record['path'])
        self.assertEqual(final['manifest']['asset_id'], candidate['manifest']['asset_id'])
        self.assertEqual(Path(final_record['path']).read_bytes(), b'restored changed texture')
        self.assertEqual(candidate['max_file'].read_bytes(), b'MAX revision 3')
        self.assertEqual(len(self.runtime.objects), 2)

    def test_legacy_202_downscale_variants_can_be_replaced(self):
        self.options = replace(self.options, texture_downscale_sizes=(1024, 512))

        def resize(source, target, size, kind, report):
            target.write_bytes(source.read_bytes() + str(size).encode('ascii'))

        with patch.object(core, 'usable_downscale_sizes', return_value=(1024, 512)), \
                patch.object(core, 'resize_texture_file', side_effect=resize):
            candidate, original, old_node = self.first_import()
            legacy = dict(candidate['manifest'])
            legacy['version'] = '2.02'
            legacy.pop('owned_files', None)
            legacy.pop('relink_materials', None)
            legacy['texture_downscale_sizes'] = [1024, 512]
            legacy_manifest = candidate['texture_folder'] / 'import_manifest.json'
            legacy_report = candidate['texture_folder'] / 'import_report.txt'
            legacy_manifest.write_text(json.dumps(legacy), encoding='utf-8')
            legacy_report.write_text('Legacy import report', encoding='utf-8')
            self.assertEqual(store.discover_assets(core, self.options)[0]['metadata_storage'], 'sidecar')
            self.source_map.write_bytes(b'legacy updated')
            core.run_import_and_convert(self.options, overwrite_resolver=lambda options, items: items[0],
                                        overwrite_confirm=lambda *args: True)
        variants = list(candidate['texture_folder'].glob('*.png'))
        self.assertEqual(len(variants), 2)
        for variant in variants:
            self.assertTrue(variant.read_bytes().startswith(b'legacy updated'))
        self.assertEqual(store.discover_assets(core, self.options)[0]['manifest']['asset_id'], legacy['asset_id'])
        self.assertFalse(legacy_manifest.exists())
        self.assertFalse(legacy_report.exists())
        self.assertEqual(store.discover_assets(core, self.options)[0]['metadata_storage'], 'filename')


if __name__ == '__main__':
    unittest.main()
