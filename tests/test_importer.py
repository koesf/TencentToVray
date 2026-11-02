import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_fbx_metadata as metadata
import tencent_importer_core as core
import tencent_batch_support as support


def png(rgb):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    return core.PNG_SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0' + bytes(rgb))) + chunk(b'IEND', b'')


def fbx(records, version=7400):
    header = b'Kaydara FBX Binary  \0\x1a\0' + struct.pack('<I', version)
    fmt, size = ('<QQQB', 25) if version >= 7500 else ('<IIIB', 13)
    def prop(value):
        if isinstance(value, int):
            return b'L' + struct.pack('<q', value)
        if isinstance(value, str):
            value = value.encode('utf-8')
            return b'S' + struct.pack('<I', len(value)) + value
        return b'R' + struct.pack('<I', len(value)) + value
    def node(record, offset):
        name, values, children = record
        name = name.encode('utf-8')
        props = b''.join(prop(p) for p in values)
        start = offset + size + len(name) + len(props)
        body = b''
        for child in children:
            body += node(child, start + len(body))
        if children:
            body += bytes(size)
        end = start + len(body)
        return struct.pack(fmt, end, len(values), len(props), len(name)) + name + props + body
    result = header
    for record in records:
        result += node(record, len(result))
    return result + bytes(size)


def material_fixture(payloads, version=7400):
    objects, connections = [], []
    for index, payload in enumerate(payloads, 1):
        mat, texture, video = index * 10, index * 10 + 1, index * 10 + 2
        objects += [('Material', [mat, f'Material{index}\0\1Material'], []),
                    ('Texture', [texture, 'base_color_texture'], []),
                    ('Video', [video, 'texture_pbr.png'], [('Content', [payload], []), ('RelativeFilename', ['texture_pbr.png'], [])])]
        connections += [('C', ['OP', texture, mat, 'DiffuseColor'], []), ('C', ['OO', video, texture], [])]
    return fbx([('Objects', [], objects), ('Connections', [], connections)], version)


class ImporterTests(unittest.TestCase):
    def test_video_ownership_with_filename_after_content_and_two_materials(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            for version in (7400, 7500):
                path = temp / f'{version}.fbx'
                path.write_bytes(material_fixture([png((255, 0, 0)), png((0, 0, 255))], version))
                maps = support.extract_material_maps(core, path, temp / str(version), core.Report())
                self.assertEqual(maps['Material1']['basecolor'].source_path.read_bytes(), png((255, 0, 0)))
                self.assertEqual(maps['Material2']['basecolor'].source_path.read_bytes(), png((0, 0, 255)))

    def test_jpeg_payload_keeps_actual_extension(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp); path = temp / 'test.fbx'
            path.write_bytes(material_fixture([b'\xff\xd8fixture\xff\xd9']))
            maps = support.extract_material_maps(core, path, temp / 'out', core.Report())
            self.assertEqual(maps['Material1']['basecolor'].source_path.suffix, '.jpg')

    def test_missing_embedded_map_does_not_search_other_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp); path = temp / 'test.fbx'
            path.write_bytes(material_fixture([b'']))
            (temp / 'texture_pbr.png').write_bytes(png((0, 255, 0)))
            with self.assertRaisesRegex(ValueError, 'nicht.*eingebettet'):
                support.extract_material_maps(core, path, temp / 'out', core.Report())

    def test_rejects_invalid_fbx(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.fbx'
            path.write_bytes(b'not FBX')
            with self.assertRaises(ValueError):
                metadata.inspect(path)

    def test_reserves_whole_asset_namespace_on_repeat(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(core, 'rt', None):
            options = core.ImportOptions(Path('asset.fbx'), 'Same Asset', '260905', Path(temp), Path(temp) / 'maps')
            a = support.reserve_asset(core, options)
            b = support.reserve_asset(core, options)
            self.assertNotEqual(a.asset_name, b.asset_name)
            self.assertNotEqual(a.texture_folder, b.texture_folder)
            self.assertEqual(a.texture_folder.parent.parent, Path(temp))

    def test_batch_error_does_not_stop_next_row(self):
        with patch.object(core, 'run_import_and_convert', side_effect=[ValueError('bad'), 'good']):
            results = core.run_batch([core.ImportOptions(Path('a.fbx'), 'First', '260916', Path('out'), Path('maps')),
                                      core.ImportOptions(Path('b.fbx'), 'Second', '260916', Path('out'), Path('maps'))])
            self.assertEqual([r['success'] for r in results], [False, True])

    def test_cancel_before_next_row(self):
        cancel = [False]
        def progress(index, status, text):
            if status == 'Fertig':
                cancel[0] = True
        with patch.object(core, 'run_import_and_convert', return_value='done') as run:
            rows = [core.ImportOptions(Path('a.fbx'), 'First', '260916', Path('out'), Path('maps')),
                    core.ImportOptions(Path('b.fbx'), 'Second', '260916', Path('out'), Path('maps'))]
            self.assertEqual(len(core.run_batch(rows, progress, lambda: cancel[0])), 1)
            run.assert_called_once()

    def test_color_policy(self):
        self.assertEqual(core.expected_bitmap_color_settings('basecolor')[::2][:2], (2, 1))
        for kind in ('roughness', 'normal', 'metalness', 'ao', 'height'):
            settings = core.expected_bitmap_color_settings(kind)
            self.assertEqual((settings[0], settings[2]), (0, 3))

    def test_generated_source_fbx_are_separate(self):
        # Two independent files deliberately use the same embedded texture name.
        # Generated fixtures keep this test independent of private source assets.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            payloads = [png((255, 0, 0)), png((0, 0, 255))]
            maps = []
            for index, payload in enumerate(payloads):
                source = root / f'source_{index}.fbx'
                source.write_bytes(material_fixture([payload]))
                maps.append(support.extract_material_maps(core, source, root / f'maps_{index}', core.Report()))
            extracted = [item['Material1']['basecolor'].source_path for item in maps]
            self.assertNotEqual(extracted[0], extracted[1])
            self.assertEqual([path.read_bytes() for path in extracted], payloads)
            self.assertNotEqual(support.sha256(extracted[0]), support.sha256(extracted[1]))


if __name__ == '__main__':
    unittest.main()
