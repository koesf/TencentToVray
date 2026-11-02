"""Compact visible identity retries collisions and preserves legacy relink data."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_importer_core as core
import tencent_batch_support as support
from test_importer_202 import FrozenDateTime, make_options


class ShortIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.options = make_options(self.root)
        self.clock = patch.object(core, 'datetime', FrozenDateTime)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_same_short_code_collision_retries_without_overwriting_existing_directory(self):
        with patch.object(support.secrets, 'choice', side_effect=list('AAAAAAAAAAAAAAAABBBBBBBB')):
            first = support.reserve_asset(core, self.options)
            marker = first.texture_folder / 'existing-map.png'
            marker.write_bytes(b'keep original')
            second = support.reserve_asset(core, self.options)
        self.assertEqual(first.asset_short_code, 'AAAAAAAA')
        self.assertEqual(second.asset_short_code, 'BBBBBBBB')
        self.assertNotEqual(first.asset_id, second.asset_id)
        self.assertEqual(marker.read_bytes(), b'keep original')
        self.assertNotEqual(first.texture_folder, second.texture_folder)

    def test_stale_max_with_same_short_code_is_never_overwritten(self):
        self.options.asset_folder.mkdir()
        existing = core.max_file_path_for_target(self.options.asset_folder, self.options.prefix,
                                                 'Test_Asset_0907_AAAAAAAA')
        existing.write_bytes(b'keep existing MAX')
        with patch.object(support.secrets, 'choice', side_effect=list('AAAAAAAABBBBBBBB')):
            allocated = support.reserve_asset(core, self.options)
        self.assertEqual(allocated.asset_short_code, 'BBBBBBBB')
        self.assertEqual(existing.read_bytes(), b'keep existing MAX')
        self.assertFalse((core.maps_folder_for_target(self.options.asset_folder, 'Test_Asset') /
                          '0907_AAAAAAAA').exists())

    def test_filename_prefix_alias_cannot_reserve_same_short_directory(self):
        with patch.object(support.secrets, 'choice', side_effect=list('AAAAAAAAAAAAAAAABBBBBBBB')):
            first = support.reserve_asset(core, replace(self.options, asset_name='Stuhl'))
            second = support.reserve_asset(core, replace(self.options, asset_name='ai_Stuhl'))
        self.assertEqual(first.asset_short_code, 'AAAAAAAA')
        self.assertEqual(second.asset_short_code, 'BBBBBBBB')
        self.assertEqual(first.texture_folder.parent, second.texture_folder.parent)

    def test_mesh_only_identity_cannot_be_reused_on_another_date(self):
        self.options.asset_folder.mkdir()
        existing = core.max_file_path_for_target(self.options.asset_folder, '250101',
                                                 'Test_Asset_0907_AAAAAAAA')
        existing.write_bytes(b'old mesh-only MAX, no map directory')
        with patch.object(support.secrets, 'choice', side_effect=list('AAAAAAAABBBBBBBB')):
            allocated = support.reserve_asset(core, self.options)
        self.assertEqual(allocated.asset_short_code, 'BBBBBBBB')
        self.assertEqual(existing.read_bytes(), b'old mesh-only MAX, no map directory')

    def test_full_uuid_stays_internal_and_all_visible_paths_share_short_code(self):
        allocated = support.reserve_asset(core, self.options)
        source = self.root / 'texture.png'
        source.write_bytes(b'image fixture')
        copied = core.copy_maps({'basecolor': core.MapHit('basecolor', source, 1000)},
                               allocated.texture_folder, allocated.prefix, allocated.asset_name,
                               True, (), core.Report())['basecolor'].target_path
        max_file = core.max_file_path_for_target(allocated.asset_folder, allocated.prefix,
                                                allocated.asset_name)
        for visible in (allocated.asset_name, allocated.texture_folder.name, copied.name, max_file.name):
            self.assertIn(allocated.asset_short_code, visible)
            self.assertNotIn(allocated.asset_id, visible)
        self.assertEqual(len(allocated.asset_short_code), 8)
        self.assertEqual(len(allocated.asset_id), 32)


if __name__ == '__main__':
    unittest.main()
