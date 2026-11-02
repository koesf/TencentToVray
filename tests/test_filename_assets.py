"""Recognize exact file names without reading or storing management metadata."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_asset_store as store
import tencent_importer_core as core


class FilenameAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.code = 'ai_Chair_1907_ABCD2345_260924'
        self.asset = 'Chair_1907_ABCD2345'
        self.max_file = self.root / (self.code + '.max')
        self.max_file.write_bytes(b'MAX bytes without importer data')
        self.folder = self.root / 'ai_Chair_maps' / '1907_ABCD2345'
        self.options = SimpleNamespace(asset_folder=self.root, asset_name='Chair', requested_asset_name='')

    def candidates(self):
        return store.discover_assets(core, self.options)

    def map(self, suffix='basecolor.png', material=None):
        self.folder.mkdir(parents=True, exist_ok=True)
        code = self.code if material is None else 'ai_' + self.asset + '_mat' + str(material).zfill(2) + '_260924'
        path = self.folder / (code + '_' + suffix)
        path.write_bytes(b'map ' + suffix.encode())
        return path

    def test_mesh_only_asset_needs_only_max_and_no_metadata(self):
        before = self.max_file.read_bytes()
        candidate = self.candidates()[0]
        self.assertEqual(candidate['metadata_storage'], 'filename')
        self.assertEqual(candidate['manifest_path'], self.max_file)
        self.assertEqual(candidate['managed_paths'], (self.max_file,))
        self.assertEqual(candidate['manifest']['materials'], {})
        self.assertEqual(candidate['manifest']['asset_id'], store.filename_asset_id(self.code))
        self.assertEqual(candidate['manifest']['source_fbx'], '')
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.max_file.read_bytes(), before)
        self.assertEqual(list(self.root.rglob('*')), [self.max_file])

    def test_texture_variants_and_material_numbers_are_inferred_exactly(self):
        small = self.map('512px_basecolor.png', material=1)
        large = self.map('2048px_basecolor.png', material=1)
        normal = self.map('normal.png', material=3)
        candidate = self.candidates()[0]
        data = candidate['manifest']
        self.assertEqual(data['_filename_material_indices'], {'Material 01': 1, 'Material 03': 3})
        self.assertEqual(data['materials']['Material 01']['basecolor']['path'], str(large))
        self.assertEqual(data['materials']['Material 03']['normal']['path'], str(normal))
        self.assertEqual(set(candidate['managed_paths']), {self.max_file, small, large, normal})

    def test_original_size_is_preferred_when_original_and_downscale_coexist(self):
        self.map('2048px_basecolor.png')
        original = self.map('basecolor.png')
        candidate = self.candidates()[0]
        self.assertEqual(candidate['manifest']['materials']['Material']['basecolor']['path'], str(original))

    def test_duplicate_kind_and_size_with_other_extension_is_ambiguous(self):
        self.map('512px_basecolor.png')
        self.map('512px_basecolor.jpg')
        self.assertEqual(self.candidates(), [])

    def test_mixed_single_and_multiple_material_conventions_are_ambiguous(self):
        self.map('normal.png')
        self.map('basecolor.png', material=1)
        self.assertEqual(self.candidates(), [])

    def test_unknown_files_folders_and_legacy_partial_reports_block_inference(self):
        self.map()
        for name in ('unrelated.png', self.code + '_basecolor_2.png',
                     self.code + '_notes.txt', 'import_report.txt', 'FAILED.txt'):
            with self.subTest(name=name):
                unknown = self.folder / name
                unknown.write_bytes(b'unrelated')
                self.assertEqual(self.candidates(), [])
                unknown.unlink()
        nested = self.folder / 'nested'
        nested.mkdir()
        self.assertEqual(self.candidates(), [])

    def test_wrong_date_prefix_and_material_zero_are_not_owned(self):
        self.folder.mkdir(parents=True)
        for name in ('ai_Chair_1907_ABCD2345_260923_basecolor.png',
                     'ai_Table_1907_ABCD2345_260924_basecolor.png',
                     'ai_Chair_1907_ABCD2345_mat00_260924_basecolor.png'):
            with self.subTest(name=name):
                path = self.folder / name
                path.write_bytes(b'foreign')
                self.assertEqual(self.candidates(), [])
                path.unlink()

    def test_corrupt_sidecar_never_falls_back_to_file_names(self):
        self.map()
        manifest = self.folder / 'import_manifest.json'
        for payload in ('{broken', '{}', '{"asset_id":"unknown"}'):
            with self.subTest(payload=payload):
                manifest.write_text(payload, encoding='utf-8')
                self.assertEqual(self.candidates(), [])

    def test_valid_legacy_sidecar_takes_precedence_and_can_migrate(self):
        self.map()
        data = dict(self.candidates()[0]['manifest'])
        data.update(version='2.03', asset_id='c' * 32, source_fbx='historical.fbx')
        sidecar, report = self.folder / 'import_manifest.json', self.folder / 'import_report.txt'
        sidecar.write_text(json.dumps(data), encoding='utf-8')
        report.write_text('old report', encoding='utf-8')
        candidate = self.candidates()[0]
        self.assertEqual(candidate['metadata_storage'], 'sidecar')
        self.assertEqual(candidate['manifest']['asset_id'], 'c' * 32)
        with store.AssetTransaction(self.root, self.folder, self.max_file, candidate) as tx:
            staged = tx.stage_dir / 'model.max'
            staged.write_bytes(b'new MAX bytes without importer data')
            tx.publish({staged: self.max_file}, remove=(sidecar, report))
        migrated = self.candidates()[0]
        self.assertEqual(migrated['metadata_storage'], 'filename')
        self.assertFalse(sidecar.exists())
        self.assertFalse(report.exists())
        self.assertFalse(any(path.suffix in ('.json', '.txt') for path in self.root.rglob('*')))

    def test_name_alias_and_moved_root_keep_same_inferred_identity(self):
        self.map()
        identity = self.candidates()[0]['manifest']['asset_id']
        moved = self.root / 'moved'
        moved.mkdir()
        shutil.copy2(self.max_file, moved / self.max_file.name)
        shutil.copytree(self.folder.parent, moved / self.folder.parent.name)
        self.options.asset_folder = moved
        self.options.asset_name = 'ai_chair'
        candidate = self.candidates()[0]
        self.assertEqual(candidate['manifest']['asset_id'], identity)
        self.assertTrue(all(store._inside(path, moved) for path in candidate['managed_paths']))

    def test_legacy_layout_probe_allows_only_recognizable_owned_map_names(self):
        path = self.map()
        data = dict(self.candidates()[0]['manifest'])
        data['version'] = '2.03'
        sidecar = self.folder / 'import_manifest.json'
        sidecar.write_text(json.dumps(data), encoding='utf-8')
        (self.folder / 'import_report.txt').write_text('report', encoding='utf-8')
        candidate = self.candidates()[0]
        with patch.object(store, '_fingerprint', side_effect=AssertionError('No repeated MAX hashing')):
            store.validate_filename_layout(core, candidate)
        renamed = path.with_name(self.code + '_oldcustomtexture.png')
        path.rename(renamed)
        item = data['materials']['Material']['basecolor']
        item.update(path=str(renamed), filename=renamed.name,
                    relative_path=renamed.relative_to(self.root).as_posix())
        data['owned_files'] = [renamed.relative_to(self.root).as_posix()]
        sidecar.write_text(json.dumps(data), encoding='utf-8')
        candidate = self.candidates()[0]
        self.assertEqual(candidate['metadata_storage'], 'sidecar')
        with self.assertRaisesRegex(store.AssetStoreError, 'als Neu importieren'):
            store.validate_filename_layout(core, candidate)

    def test_legacy_long_identity_is_reused_without_extra_file(self):
        self.max_file.unlink()
        identity = 'a' * 32
        code = 'ai_Chair_1907_' + identity + '_260924'
        max_file = self.root / (code + '.max')
        max_file.write_bytes(b'old MAX')
        candidate = self.candidates()[0]
        self.assertEqual(candidate['manifest']['asset_id'], identity)
        self.assertEqual(candidate['manifest']['asset_short_code'], '')

    def test_duplicate_identity_with_another_date_blocks_both_candidates(self):
        other = self.root / 'ai_Chair_1907_ABCD2345_260923.max'
        other.write_bytes(b'other MAX')
        self.assertEqual(self.candidates(), [])

    def test_invalid_time_and_date_do_not_become_assets(self):
        self.max_file.unlink()
        for name in ('ai_Chair_2507_ABCD2345_260924.max',
                     'ai_Chair_1907_ABCD2345_260231.max',
                     'ai_Chair_1907_ABCD2345_other.max'):
            path = self.root / name
            path.write_bytes(b'MAX')
            self.assertEqual(self.candidates(), [])
            path.unlink()

    def test_changed_map_bytes_invalidate_confirmation(self):
        path = self.map()
        candidate = self.candidates()[0]
        old = path.stat()
        path.write_bytes(b'X' * old.st_size)
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaises(store.AssetChangedError):
            with store.AssetTransaction(self.root, self.folder, self.max_file, candidate):
                pass

    def test_added_file_after_confirmation_aborts_before_any_write(self):
        self.map()
        candidate = self.candidates()[0]
        with store.AssetTransaction(self.root, self.folder, self.max_file, candidate) as tx:
            staged = tx.stage_dir / 'model.max'
            staged.write_bytes(b'new MAX')
            self.map('roughness.png')
            with self.assertRaises(store.AssetChangedError):
                tx.publish({staged: self.max_file})
        self.assertEqual(self.max_file.read_bytes(), b'MAX bytes without importer data')

    def test_publication_rolls_back_maps_and_max_without_metadata_file(self):
        path = self.map()
        originals = {path: path.read_bytes(), self.max_file: self.max_file.read_bytes()}
        candidate = self.candidates()[0]
        with self.assertRaisesRegex(RuntimeError, 'scene'):
            with store.AssetTransaction(self.root, self.folder, self.max_file, candidate) as tx:
                staged_max, staged_map = tx.stage_dir / 'model.max', tx.stage_dir / 'map.png'
                staged_max.write_bytes(b'new MAX')
                staged_map.write_bytes(b'new map')
                tx.publish({staged_max: self.max_file, staged_map: path})
                raise RuntimeError('scene failed')
        for path, payload in originals.items():
            self.assertEqual(path.read_bytes(), payload)
        self.assertFalse(any(path.suffix in ('.json', '.txt') for path in self.root.rglob('*')))


if __name__ == '__main__':
    unittest.main()
