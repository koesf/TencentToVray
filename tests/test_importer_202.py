"""Regression coverage for unique asset identities and optional import stages."""
import json
import sys
import tempfile
import unittest
import uuid
from contextlib import ExitStack, nullcontext
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_batch_support as support
import tencent_importer_core as core
import tencent_asset_store as store
import tencent_max_metadata as legacy_metadata


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 16, 9, 7, 12, tzinfo=tz)


def make_options(root, **changes):
    source = root / 'source.fbx'
    source.write_bytes(b'FBX fixture: imported by the runtime stub')
    options = core.ImportOptions(
        source, 'Test Asset', '260916', root / 'output', root / 'maps',
        create_layer=False,
    )
    return replace(options, **changes)


class RuntimeStub:
    """Model scene boundaries while making destructive geometry calls visible."""
    GeometryClass = 'geometry'

    def __init__(self):
        self.objects = []
        self.selection = []
        self.saved_nodes = []
        self.saved_materials = []
        self.VRayMtl = Mock(return_value=SimpleNamespace(name='VRay material'))
        self.VRayBitmap = Mock(return_value=object())
        self.getClassInstances = Mock(return_value=[])
        self.getPolygonCount = Mock(return_value=(100, 80))
        self.resetXForm = Mock(side_effect=AssertionError('Unexpected Reset XForm'))
        self.collapseStack = Mock(side_effect=AssertionError('Unexpected stack collapse'))
        self.addModifier = Mock(side_effect=AssertionError('Unexpected modifier'))
        self.tyOptimize = Mock(side_effect=AssertionError('Unexpected tyOptimize dependency'))
        self.setUserProp = Mock()
        self.doesUserPropExist = Mock(return_value=False)
        self.deleteUserProp = Mock()
        self.select = Mock()
        self.delete = Mock()

    def superClassOf(self, node):
        return node.kind

    def isProperty(self, node, name):
        return hasattr(node, str(name))

    def Name(self, name):
        return name

    def isValidNode(self, node):
        return node in self.objects

    def Array(self, *nodes):
        return list(nodes)

    def saveNodes(self, nodes, target, **kwargs):
        self.saved_nodes = list(nodes)
        self.saved_materials = [node.material for node in nodes]
        Path(target).write_bytes(b'asset-only MAX fixture')
        return True


def mesh(name):
    return SimpleNamespace(
        name=name,
        kind=RuntimeStub.GeometryClass,
        material=SimpleNamespace(name='Original FBX material'),
        modifiers=[object(), object()],
        transform=object(),
    )


class UniqueIdentityTests(unittest.TestCase):
    def test_asset_name_is_required_and_must_be_a_simple_name(self):
        for invalid in ('', '   ', '...---___', 'folder/name', r'folder\name', 'x' * 41):
            with self.subTest(name=invalid), self.assertRaises(ValueError) as error:
                core.validate_asset_name(invalid)
            self.assertRegex(str(error.exception), r'Name|Namen|Zeichen')
        self.assertEqual(core.validate_asset_name('  Test Asset  '), 'Test_Asset')
        self.assertEqual(core.validate_asset_name('Tür groß'), 'Tuer_gross')

    def test_invalid_name_fails_before_reserving_any_directory(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, 'rt', None):
            options = make_options(Path(temp), asset_name='   ')
            with self.assertRaises(ValueError):
                support.reserve_asset(core, options)
            self.assertFalse(options.asset_folder.exists())

    def test_frozen_time_and_different_targets_still_have_distinct_identities(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, 'rt', None), \
                patch.object(core, 'datetime', FrozenDateTime):
            root = Path(temp)
            options = make_options(root)
            reserved = [support.reserve_asset(core, replace(options, asset_folder=root / folder))
                        for folder in ('first', 'second')]
            self.assertNotEqual(reserved[0].asset_name, reserved[1].asset_name)
            for item in reserved:
                self.assertRegex(item.asset_name, r'^Test_Asset_0907_[A-HJ-NP-Z2-9]{8}$')
                self.assertEqual(uuid.UUID(hex=item.asset_id).version, 5)
                self.assertIn(item.asset_short_code, item.asset_name)
                self.assertNotIn(item.asset_id, item.asset_name)
                self.assertEqual(item.requested_asset_name, 'Test_Asset')
                self.assertEqual(item.import_time_code, '0907')
                self.assertEqual(item.texture_folder.parent,
                                 core.maps_folder_for_target(item.asset_folder, 'Test_Asset'))

    def test_repeated_import_does_not_overwrite_maps_or_max_file(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, 'rt', None), \
                patch.object(core, 'datetime', FrozenDateTime):
            options = make_options(Path(temp))
            first = support.reserve_asset(core, options)
            first_map = first.texture_folder / 'preserved.png'
            first_map.write_bytes(b'first texture')
            first_max = core.max_file_path_for_target(first.asset_folder, first.prefix, first.asset_name)
            first_max.write_bytes(b'first MAX')
            second = support.reserve_asset(core, options)
            self.assertNotEqual(first.texture_folder, second.texture_folder)
            self.assertNotEqual(first.asset_name, second.asset_name)
            self.assertEqual(first_map.read_bytes(), b'first texture')
            self.assertEqual(first_max.read_bytes(), b'first MAX')

    def test_map_basenames_can_be_relinked_after_assets_are_moved_together(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, 'rt', None), \
                patch.object(core, 'datetime', FrozenDateTime):
            root = Path(temp)
            options = make_options(root)
            source = root / 'texture_pbr.png'
            source.write_bytes(b'original embedded image')
            names = []
            for folder in ('first', 'second'):
                item = support.reserve_asset(core, replace(options, asset_folder=root / folder))
                copied = core.copy_maps(
                    {'basecolor': core.MapHit('basecolor', source, 1000)},
                    item.texture_folder, item.prefix, item.asset_name, True, (), core.Report(),
                )['basecolor'].target_path
                names.append(copied.name)
                self.assertIn(item.asset_short_code, copied.name)
                self.assertNotIn(item.asset_id, copied.name)
                self.assertIn('_0907_', copied.name)
                self.assertEqual(copied.read_bytes(), source.read_bytes())
            self.assertEqual(len(set(names)), 2)


class OptionalImportStageTests(unittest.TestCase):
    def test_legacy_importer_annotations_are_removed_without_touching_user_properties(self):
        runtime = RuntimeStub()
        node = mesh('Mesh')
        properties = {'TencentAssetID': 'legacy', 'TencentSourceFBX': 'old path',
                      'Designer': 'User value', 'TencentCustomUserField': 'User value'}
        runtime.doesUserPropExist.side_effect = lambda node, key: key in properties
        runtime.deleteUserProp.side_effect = lambda node, key: properties.pop(key)
        with patch.object(core, 'rt', runtime):
            support.clear_legacy_management_props(core, [node])
        self.assertEqual(properties, {'Designer': 'User value', 'TencentCustomUserField': 'User value'})
        runtime.setUserProp.assert_not_called()

    def test_disabled_optimizer_preserves_geometry_transform_and_modifier_stack(self):
        with tempfile.TemporaryDirectory() as temp:
            options = make_options(Path(temp), optimize_enabled=False, pre_optimize_80=True)
            runtime = RuntimeStub()
            node = mesh('Mesh')
            original_modifiers = list(node.modifiers)
            original_transform = node.transform
            with patch.object(core, 'rt', runtime):
                support.optimize_nodes(core, [node], options, core.Report())
            self.assertEqual(node.modifiers, original_modifiers)
            self.assertIs(node.transform, original_transform)
            for operation in (runtime.tyOptimize, runtime.resetXForm, runtime.collapseStack,
                              runtime.addModifier):
                operation.assert_not_called()

    def run_stub_import(self, options, runtime, nodes, **extra_patches):
        def import_nodes(*args):
            runtime.objects.extend(nodes)
            return nodes

        patches = {
            'rt': runtime,
            'pymxs': SimpleNamespace(redraw=lambda enabled: nullcontext()),
            'import_fbx_and_get_nodes': Mock(side_effect=import_nodes),
            'rename_nodes': Mock(),
            'create_layer_for_nodes': Mock(),
        }
        patches.update(extra_patches)
        with ExitStack() as stack:
            stack.enter_context(patch.object(legacy_metadata, 'write_manifest',
                side_effect=AssertionError('No importer metadata may be written into MAX'), create=True))
            stack.enter_context(patch.object(legacy_metadata, 'remove_manifest', return_value=False, create=True))
            for name, value in patches.items():
                stack.enter_context(patch.object(core, name, value))
            result = support.run_import(core, options)
        saved_assets = list(options.asset_folder.glob('*.max'))
        self.assertEqual(len(saved_assets), 1)
        self.assertEqual(saved_assets[0].read_bytes(), b'asset-only MAX fixture')
        runtime.setUserProp.assert_not_called()
        self.assertEqual(list(options.asset_folder.rglob('*.json')), [])
        self.assertEqual(list(options.asset_folder.rglob('*.txt')), [])
        candidates = store.discover_assets(core, options)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['metadata_storage'], 'filename')
        return result, candidates[0]['manifest']

    def test_mesh_only_import_saves_without_materials_textures_or_plugin_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            options = make_options(
                Path(temp), optimize_enabled=False, import_textures=False,
                replace_imported_material=False, create_vray_bitmap_nodes=False,
            )
            runtime = RuntimeStub()
            runtime.VRayMtl.side_effect = AssertionError('V-Ray must be optional')
            runtime.VRayBitmap.side_effect = AssertionError('V-Ray must be optional')
            runtime.getClassInstances.side_effect = AssertionError('No V-Ray scene query')
            nodes = [mesh('Mesh 1'), mesh('Mesh 2')]
            original_stacks = [list(node.modifiers) for node in nodes]
            with patch.object(support, 'extract_material_maps') as extract, \
                    patch.object(support, 'verify_material') as verify:
                copy_maps = Mock(side_effect=AssertionError('Unexpected map copy'))
                build_material = Mock(side_effect=AssertionError('Unexpected V-Ray material'))
                report, manifest = self.run_stub_import(
                    options, runtime, nodes, copy_maps=copy_maps, build_vray_material=build_material,
                )
            extract.assert_not_called()
            verify.assert_not_called()
            copy_maps.assert_not_called()
            build_material.assert_not_called()
            runtime.VRayMtl.assert_not_called()
            runtime.VRayBitmap.assert_not_called()
            runtime.tyOptimize.assert_not_called()
            runtime.resetXForm.assert_not_called()
            runtime.collapseStack.assert_not_called()
            self.assertEqual(runtime.saved_nodes, nodes)
            self.assertEqual(runtime.saved_materials, [None, None])
            self.assertEqual([node.modifiers for node in nodes], original_stacks)
            self.assertEqual(manifest['materials'], {})
            self.assertIn('Optimizer: Aus', report)
            self.assertIn('Texturen: Aus', report)
            self.assertTrue(Path(manifest['max_file']).is_file())
            self.assertEqual(manifest['requested_asset_name'], 'Test_Asset')
            self.assertIn(manifest['asset_short_code'], Path(manifest['max_file']).name)
            self.assertNotIn(manifest['asset_id'], Path(manifest['max_file']).name)
            self.assertEqual(uuid.UUID(hex=manifest['asset_id']).version, 5)

    def test_texture_import_still_works_with_optimizer_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            options = make_options(Path(temp), optimize_enabled=False, import_textures=True)
            runtime = RuntimeStub()
            nodes = [mesh('Mesh')]
            vray_material = SimpleNamespace(name='Converted V-Ray material')
            source = Path(temp) / 'texture_pbr.png'
            source.write_bytes(b'embedded image fixture')
            maps = {'Material': {'basecolor': core.MapHit('basecolor', source, 1000)}}
            with patch.object(support, 'extract_material_maps', return_value=maps), \
                    patch.object(support, 'verify_material') as verify:
                build_material = Mock(return_value=vray_material)
                report, manifest = self.run_stub_import(
                    options, runtime, nodes, build_vray_material=build_material,
                )
            build_material.assert_called_once()
            verify.assert_called_once()
            self.assertIs(runtime.saved_materials[0], vray_material)
            runtime.tyOptimize.assert_not_called()
            runtime.resetXForm.assert_not_called()
            runtime.collapseStack.assert_not_called()
            self.assertIn('Optimizer: Aus', report)
            self.assertIn('Texturen: An', report)
            recorded_map = manifest['materials']['Material']['basecolor']
            self.assertEqual(recorded_map['filename'], Path(recorded_map['path']).name)
            self.assertIn(manifest['asset_short_code'], recorded_map['filename'])
            self.assertNotIn(manifest['asset_id'], recorded_map['filename'])
            self.assertEqual((options.asset_folder / recorded_map['relative_path']).resolve(),
                             Path(recorded_map['path']).resolve())
            self.assertEqual(Path(recorded_map['path']).read_bytes(), source.read_bytes())

    def test_optimizer_can_still_run_for_mesh_only_import(self):
        with tempfile.TemporaryDirectory() as temp:
            options = make_options(
                Path(temp), optimize_enabled=True, import_textures=False, pre_optimize_80=False,
            )
            runtime = RuntimeStub()
            runtime.VRayMtl.side_effect = AssertionError('V-Ray must be optional')
            runtime.VRayBitmap.side_effect = AssertionError('V-Ray must be optional')
            runtime.tyOptimize = Mock(side_effect=SimpleNamespace)
            runtime.resetXForm = Mock()
            runtime.addModifier = Mock(side_effect=lambda node, modifier: node.modifiers.append(modifier))
            nodes = [mesh('Mesh')]
            report, manifest = self.run_stub_import(options, runtime, nodes)
            self.assertTrue(runtime.tyOptimize.called)
            runtime.resetXForm.assert_called_once_with(nodes[0])
            runtime.addModifier.assert_called_once()
            runtime.collapseStack.assert_not_called()
            self.assertEqual(nodes[0].modifiers[-1].proOptimizerVertexPercent, options.optimize_percent)
            self.assertEqual(runtime.saved_materials, [None])
            self.assertEqual(manifest['materials'], {})
            self.assertIn('Optimizer: An', report)
            self.assertIn('Texturen: Aus', report)

    def test_mesh_only_scan_does_not_require_embedded_maps(self):
        with tempfile.TemporaryDirectory() as temp:
            options = make_options(Path(temp), import_textures=False)
            with patch.object(support, 'extract_material_maps') as extract:
                report = support.scan(core, options)
            extract.assert_not_called()
            self.assertIn('ohne Material', report)


if __name__ == '__main__':
    unittest.main()
