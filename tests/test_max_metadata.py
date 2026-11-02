"""Windows OLE round-trip/locking tests with private temporary compound files."""
import ctypes
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tencent_max_metadata as metadata
from metadata_test_support import write_manifest


@unittest.skipUnless(os.name == 'nt' and ctypes.sizeof(ctypes.c_void_p) == 8,
                     'Native MAX property storage requires 64-bit Windows')
class NativeMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'asset.max'
        ole = ctypes.OleDLL('ole32')
        ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        ole.CoInitializeEx.restype = ctypes.c_long
        initialized = ole.CoInitializeEx(None, 2)
        storage = ctypes.c_void_p()
        try:
            ole.StgCreateDocfile.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                           ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
            ole.StgCreateDocfile.restype = ctypes.c_long
            metadata._check(ole.StgCreateDocfile(str(self.path), 0x1012, 0,
                                                ctypes.byref(storage)), 'Create test storage')
        finally:
            metadata._release(storage)
            if initialized >= 0:
                ole.CoUninitialize()

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def set_raw_property(self, raw, variant_type=30):
        buffer = ctypes.create_string_buffer(raw)
        value = metadata._PropVariant()
        value.vt = variant_type
        value.text = ctypes.cast(buffer, ctypes.c_void_p).value if variant_type == 30 else 123
        spec = metadata._property_spec()
        with metadata._storage(self.path, write=True, _create_missing=True) as (_ole, properties):
            metadata._check(metadata._call(properties, 4,
                            [ctypes.c_uint32, ctypes.POINTER(metadata._PropSpec),
                             ctypes.POINTER(metadata._PropVariant), ctypes.c_uint32],
                            1, ctypes.byref(spec), ctypes.byref(value), 2), 'Write fixture')

    def test_absent_does_not_modify_file_or_create_sidecars(self):
        before = self.digest()
        self.assertIsNone(metadata.read_manifest(self.path))
        self.assertEqual(before, self.digest())
        self.assertEqual([self.path], list(self.root.iterdir()))

    def test_roundtrip_unicode_nested_and_no_sidecars(self):
        manifest = {'asset': 'Stuhl\u00fc\u00df\u8a66\U0001f600', 'materials': {'diffuse': 'folder/map.png'},
                    'asset_id': 'f' * 32, 'number': 1.25, 'values': [None, False, 23]}
        write_manifest(self.path, manifest)
        self.assertEqual(manifest, metadata.read_manifest(self.path))
        self.assertEqual([self.path], list(self.root.iterdir()))

    def test_large_metadata_then_short_update(self):
        manifest = {'large': 'x' * 200000}
        write_manifest(self.path, manifest)
        self.assertEqual(manifest, metadata.read_manifest(self.path))
        write_manifest(self.path, {'revision': 2})
        self.assertEqual({'revision': 2}, metadata.read_manifest(self.path))

    def test_unrelated_custom_property_preserved(self):
        with patch.object(metadata, 'PROPERTY_NAME', 'OtherApplication.Property'):
            write_manifest(self.path, {'keep': 'Other application data'})
        self.assertIsNone(metadata.read_manifest(self.path))
        write_manifest(self.path, {'our': 'data'})
        with patch.object(metadata, 'PROPERTY_NAME', 'OtherApplication.Property'):
            self.assertEqual({'keep': 'Other application data'}, metadata.read_manifest(self.path))

    def test_moving_and_copying_file_retains_metadata(self):
        write_manifest(self.path, {'portable': True})
        moved = self.root / 'moved' / 'renamed.max'
        moved.parent.mkdir()
        shutil.copyfile(self.path, moved)
        self.assertEqual({'portable': True}, metadata.read_manifest(moved))

    def test_write_lock_prevents_read_and_write(self):
        write_manifest(self.path, {'revision': 1})
        with metadata._storage(self.path, write=True):
            with self.assertRaises(metadata.MetadataError):
                metadata.read_manifest(self.path)
            with self.assertRaises(metadata.MetadataError):
                write_manifest(self.path, {'revision': 2})
        self.assertEqual({'revision': 1}, metadata.read_manifest(self.path))

    def test_read_lock_allows_read_but_prevents_write(self):
        write_manifest(self.path, {'revision': 1})
        with metadata._storage(self.path):
            self.assertEqual({'revision': 1}, metadata.read_manifest(self.path))
            with self.assertRaises(metadata.MetadataError):
                write_manifest(self.path, {'revision': 2})

    def test_invalid_and_missing_files_fail_closed(self):
        for path in (self.root / 'missing.max', self.root / 'broken.max'):
            if path.name == 'broken.max':
                path.write_bytes(b'This is not a MAX file')
            with self.subTest(path=path.name), self.assertRaises(metadata.MetadataError):
                metadata.read_manifest(path)

    def test_json_scalar_array_broken_nonfinite_fail_closed(self):
        for raw in (b'[]', b'null', b'"Text"', b'{broken}', b'{"number":NaN}',
                    b'{"number":Infinity}', b'{"number":-Infinity}', b'\xff'):
            with self.subTest(raw=raw):
                self.set_raw_property(raw)
                with self.assertRaises(metadata.MetadataError):
                    metadata.read_manifest(self.path)

    def test_non_string_property_fails_closed(self):
        self.set_raw_property(b'', variant_type=19)  # VT_UI4
        with self.assertRaises(metadata.MetadataError):
            metadata.read_manifest(self.path)

    def test_oversized_input_rejected_before_write(self):
        before = self.digest()
        with patch.object(metadata, 'MAX_MANIFEST_BYTES', 32):
            with self.assertRaises(metadata.MetadataError):
                write_manifest(self.path, {'huge': 'x' * 100})
        self.assertEqual(before, self.digest())

    def test_oversized_property_stream_fails_closed(self):
        write_manifest(self.path, {'large': 'x' * 20000})
        with patch.object(metadata, 'MAX_MANIFEST_BYTES', 100):
            with self.assertRaises(metadata.MetadataError):
                metadata.read_manifest(self.path)

    def test_unserializable_inputs_rejected_before_write(self):
        before = self.digest()
        for value in ([], {'nan': float('nan')}, {'set': {1, 2}}, {1: 'integer key'}, {'tuple': (1, 2)}):
            with self.subTest(value=value), self.assertRaises(metadata.MetadataError):
                write_manifest(self.path, value)
        self.assertEqual(before, self.digest())

    def test_production_has_no_metadata_writer(self):
        self.assertFalse(hasattr(metadata, 'write_manifest'))

    def test_cleanup_without_properties_is_read_only(self):
        before = self.digest()
        self.assertFalse(metadata.remove_manifest(self.path))
        self.assertEqual(before, self.digest())

    def test_cleanup_with_other_properties_but_no_manifest_is_read_only(self):
        with patch.object(metadata, 'PROPERTY_NAME', 'UnrelatedApp.Property'):
            write_manifest(self.path, {'keep': 123})
        before = self.digest()
        self.assertFalse(metadata.remove_manifest(self.path))
        self.assertEqual(before, self.digest())

    def test_cleanup_removes_known_property_and_preserves_neighbors(self):
        others = ('UnrelatedApp.Property', 'TencentToVray.Manifest.v10', 'TencentToVray.Other')
        for name in others:
            with patch.object(metadata, 'PROPERTY_NAME', name):
                write_manifest(self.path, {'keep': name})
        write_manifest(self.path, {'legacy': 'remove this'})
        self.assertTrue(metadata.remove_manifest(self.path))
        self.assertIsNone(metadata.read_manifest(self.path))
        for name in others:
            with patch.object(metadata, 'PROPERTY_NAME', name):
                self.assertEqual({'keep': name}, metadata.read_manifest(self.path))
        before = self.digest()
        self.assertFalse(metadata.remove_manifest(self.path))
        self.assertEqual(before, self.digest())

    def test_cleanup_handles_invalid_legacy_value_without_parsing_it(self):
        for raw in (b'{broken json', b'null', b'\xff'):
            self.set_raw_property(raw)
            self.assertTrue(metadata.remove_manifest(self.path))
            self.assertIsNone(metadata.read_manifest(self.path))
        self.set_raw_property(b'', variant_type=19)
        self.assertTrue(metadata.remove_manifest(self.path))
        self.assertIsNone(metadata.read_manifest(self.path))

    def test_cleanup_locked_file_fails_closed(self):
        write_manifest(self.path, {'legacy': 'retain on failure'})
        with metadata._storage(self.path):
            with self.assertRaises(metadata.MetadataError):
                metadata.remove_manifest(self.path)
        self.assertEqual({'legacy': 'retain on failure'}, metadata.read_manifest(self.path))


if __name__ == '__main__':
    unittest.main()
