"""Read-only mesh recognition from FBX, independent of Qt and 3ds Max."""
from dataclasses import dataclass
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass
class ArrayValue:
    tag: str
    values: tuple
    compressed: bool = True


def binary_fbx(records, version=7400):
    header = b'Kaydara FBX Binary  \0\x1a\0' + struct.pack('<I', version)
    fmt, size = ('<QQQB', 25) if version >= 7500 else ('<IIIB', 13)

    def prop(value):
        if isinstance(value, ArrayValue):
            raw = struct.pack('<' + {'l': 'q'}.get(value.tag, value.tag) * len(value.values), *value.values)
            payload = zlib.compress(raw) if value.compressed else raw
            return value.tag.encode() + struct.pack('<III', len(value.values), int(value.compressed), len(payload)) + payload
        if isinstance(value, int):
            return b'L' + struct.pack('<q', value)
        if isinstance(value, float):
            return b'D' + struct.pack('<d', value)
        if isinstance(value, str):
            encoded = value.encode('utf-8')
            return b'S' + struct.pack('<I', len(encoded)) + encoded
        return b'R' + struct.pack('<I', len(value)) + value

    def node(record, offset):
        name, values, children = record
        name = name.encode('utf-8')
        props = b''.join(prop(value) for value in values)
        start = offset + size + len(name) + len(props)
        body = b''
        for child in children:
            body += node(child, start + len(body))
        if children:
            body += bytes(size)
        return struct.pack(fmt, start + len(body), len(values), len(props), len(name)) + name + props + body

    result = header
    for record in records:
        result += node(record, len(result))
    return result + bytes(size)


def mesh_records(vertices=None, indices=None, translate=(0., 0., 0.), geometry_id=10, model_id=20,
                 compressed=True, label='Chair'):
    vertices = vertices if vertices is not None else (0., 0., 0., 1., 0., 0., 0., 1., 0.)
    indices = indices if indices is not None else (0, 1, -3)
    objects = [
        ('Geometry', [geometry_id, 'Geometry::' + label, 'Mesh'], [
            ('Vertices', [ArrayValue('d', tuple(vertices), compressed)], []),
            ('PolygonVertexIndex', [ArrayValue('i', tuple(indices), compressed)], []),
        ]),
        ('Model', [model_id, 'Model::' + label, 'Mesh'], [
            ('Properties70', [], [('P', ['Lcl Translation', 'Lcl Translation', '', 'A', *translate], [])]),
        ]),
    ]
    links = [('C', ['OO', geometry_id, model_id], []), ('C', ['OO', model_id, 0], [])]
    return objects, links


def mesh_file(objects, links, version=7400):
    settings = [('P', [name, 'int', 'Integer', '', value], []) for name, value in
                [('UpAxis', 2), ('UpAxisSign', 1), ('FrontAxis', 1), ('FrontAxisSign', -1),
                 ('CoordAxis', 0), ('CoordAxisSign', 1)]]
    return binary_fbx([('GlobalSettings', [], [('Properties70', [], settings)]),
                       ('Objects', [], objects), ('Connections', [], links)], version)


class MeshPreviewTests(unittest.TestCase):
    def setUp(self):
        import tencent_mesh_preview
        self.loader = tencent_mesh_preview
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'source.fbx'

    def write(self, vertices=None, indices=None, **kwargs):
        version = kwargs.pop('version', 7400)
        objects, links = mesh_records(vertices, indices, **kwargs)
        self.path.write_bytes(mesh_file(objects, links, version))
        return self.loader.load_mesh_preview(self.path)

    def test_binary_32_and_64_bit_headers_and_both_array_encodings(self):
        for version in (7400, 7500):
            for compressed in (False, True):
                with self.subTest(version=version, compressed=compressed):
                    result = self.write(version=version, compressed=compressed)
                    self.assertEqual(result.mesh_count, 1)
                    self.assertEqual(result.source_vertex_count, 3)
                    self.assertEqual(result.source_triangle_count, 1)
                    self.assertEqual(len(result.triangles), 1)
                    self.assertTrue(all(math.isfinite(value) for p in result.vertices for value in p))

    def test_preview_centres_mesh_and_normalizes_longest_dimension(self):
        result = self.write(translate=(100., -300., 25.))
        spans = []
        for axis in range(3):
            lo = min(point[axis] for point in result.vertices)
            hi = max(point[axis] for point in result.vertices)
            self.assertAlmostEqual(lo + hi, 0., places=5)
            spans.append(hi - lo)
        self.assertAlmostEqual(max(spans), 2., places=5)

    def test_all_meshes_and_model_translations_contribute_to_overview(self):
        objects, links = mesh_records()
        second, second_links = mesh_records(translate=(10., 0., 0.), geometry_id=11, model_id=21, label='Table')
        self.path.write_bytes(mesh_file(objects + second, links + second_links))
        result = self.loader.load_mesh_preview(self.path)
        self.assertEqual(result.mesh_count, 2)
        self.assertEqual(result.source_triangle_count, 2)
        span_x = max(p[0] for p in result.vertices) - min(p[0] for p in result.vertices)
        span_y = max(p[1] for p in result.vertices) - min(p[1] for p in result.vertices)
        self.assertGreater(span_x, span_y * 5)
        self.assertTrue(any('Chair' in name for name in result.model_names))
        self.assertTrue(any('Table' in name for name in result.model_names))

    def test_quad_is_triangulated_without_dropping_its_corner(self):
        result = self.write(vertices=(0., 0., 0., 2., 0., 0., 2., 2., 0., 0., 2., 0.),
                            indices=(0, 1, 2, -4))
        self.assertEqual(result.source_triangle_count, 2)
        self.assertEqual(len(result.triangles), 2)
        self.assertEqual(len({index for tri in result.triangles for index in tri}), 4)

    def test_source_and_parent_directory_are_unchanged(self):
        self.write()
        before_bytes = self.path.read_bytes()
        before_stat = self.path.stat()
        before_paths = set(Path(self.temp.name).rglob('*'))
        self.loader.load_mesh_preview(self.path)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(self.path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(set(Path(self.temp.name).rglob('*')), before_paths)

    def test_invalid_indices_are_rejected_instead_of_reading_unrelated_vertices(self):
        for indices in ((0, 1, -31), (0, 1, 2)):
            with self.subTest(indices=indices), self.assertRaises(ValueError):
                self.write(indices=indices)

    def test_nonfinite_geometry_is_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.write(vertices=(value, 0., 0., 1., 0., 0., 0., 1., 0.))

    def test_short_corrupt_and_non_fbx_files_fail_clearly(self):
        for data in (b'', b'not FBX', b'Kaydara FBX Binary  \0\x1a\0'):
            with self.subTest(data=data):
                self.path.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.loader.load_mesh_preview(self.path)

    def test_cancelled_preview_stops_without_outputs(self):
        self.write()
        with self.assertRaises(self.loader.PreviewCancelled):
            self.loader.load_mesh_preview(self.path, cancelled=lambda: True)
        self.assertEqual([p.name for p in Path(self.temp.name).iterdir()], ['source.fbx'])

    def test_dense_grid_preview_respects_geometry_budget(self):
        side = 70
        vertices = [value for y in range(side) for x in range(side)
                    for value in (float(x), float(y), math.sin(x * .1) * math.cos(y * .1))]
        faces = []
        for y in range(side - 1):
            for x in range(side - 1):
                a = y * side + x
                faces.extend((a, a + 1, -(a + side + 2), a, a + side + 1, -(a + side + 1)))
        objects, links = mesh_records(vertices, faces)
        self.path.write_bytes(mesh_file(objects, links))
        result = self.loader.load_mesh_preview(self.path, max_triangles=500, max_vertices=600)
        self.assertLessEqual(len(result.triangles), 500)
        self.assertLessEqual(len(result.vertices), 600)
        self.assertGreater(len(result.triangles), 20)
        self.assertEqual(result.source_triangle_count, 2 * (side - 1) ** 2)
        self.assertTrue(all(0 <= i < len(result.vertices) for tri in result.triangles for i in tri))

    def test_64_bit_polygon_indices_are_portable_on_windows(self):
        objects, links = mesh_records()
        objects[0][2][1][1][0] = ArrayValue('l', (0, 1, -3))
        self.path.write_bytes(mesh_file(objects, links, 7500))
        result = self.loader.load_mesh_preview(self.path)
        self.assertEqual(len(result.triangles), 1)

    def test_one_geometry_instanced_by_two_models_appears_twice(self):
        objects, links = mesh_records()
        objects.append(('Model', [21, 'Model::SecondInstance', 'Mesh'], [
            ('Properties70', [], [('P', ['Lcl Translation', 'Lcl Translation', '', 'A', 10., 0., 0.], [])])]))
        links.extend([('C', ['OO', 10, 21], []), ('C', ['OO', 21, 0], [])])
        self.path.write_bytes(mesh_file(objects, links))
        result = self.loader.load_mesh_preview(self.path)
        self.assertEqual(result.mesh_count, 2)
        self.assertEqual(len(result.triangles), 2)

    def test_model_parent_translation_and_own_geometric_transform_are_separate(self):
        objects, links = mesh_records()
        second, more_links = mesh_records(geometry_id=11, model_id=21, label='Child')
        objects[1][2][0][2].append(('P', ['GeometricTranslation', 'Vector3D', 'Vector', '', 10., 0., 0.], []))
        links = [link for link in links + more_links if link[1][1] != 21]
        links.append(('C', ['OO', 21, 20], []))
        self.path.write_bytes(mesh_file(objects + second, links))
        result = self.loader.load_mesh_preview(self.path)
        span_x = max(p[0] for p in result.vertices) - min(p[0] for p in result.vertices)
        span_y = max(p[1] for p in result.vertices) - min(p[1] for p in result.vertices)
        self.assertGreater(span_x, span_y * 5, 'Parent geometric transform must not move its child mesh')

    def test_cyclic_model_hierarchy_fails_instead_of_recursing_forever(self):
        objects, links = mesh_records()
        objects.append(('Model', [21, 'Model::Parent', 'Null'], []))
        links.extend([('C', ['OO', 20, 21], []), ('C', ['OO', 21, 20], [])])
        self.path.write_bytes(mesh_file(objects, links))
        with self.assertRaisesRegex(ValueError, '(?i)hierarchie'):
            self.loader.load_mesh_preview(self.path)

    def test_y_up_fbx_is_displayed_standing_along_z_axis(self):
        objects, links = mesh_records(vertices=(0., 0., 0., 1., 0., 0., 0., 5., 0.))
        settings = [('P', [name, 'int', 'Integer', '', value], []) for name, value in
                    [('UpAxis', 1), ('UpAxisSign', 1), ('FrontAxis', 2), ('FrontAxisSign', 1),
                     ('CoordAxis', 0), ('CoordAxisSign', 1)]]
        self.path.write_bytes(binary_fbx([('GlobalSettings', [], [('Properties70', [], settings)]),
                                          ('Objects', [], objects), ('Connections', [], links)]))
        result = self.loader.load_mesh_preview(self.path)
        spans = [max(p[axis] for p in result.vertices) - min(p[axis] for p in result.vertices) for axis in range(3)]
        self.assertGreater(spans[2], spans[0] * 4)
        self.assertAlmostEqual(spans[1], 0.)

    def test_compressed_array_must_match_its_declared_length(self):
        objects, links = mesh_records()
        data = mesh_file(objects, links)
        raw = zlib.compress(struct.pack('<' + 'd' * 9, 0., 0., 0., 1., 0., 0., 0., 1., 0.))
        marker = b'd' + struct.pack('<III', 9, 1, len(raw)) + raw
        self.assertIn(marker, data)
        # Only alter the declared element count, keeping every record boundary valid.
        self.path.write_bytes(data.replace(marker, b'd' + struct.pack('<III', 6, 1, len(raw)) + raw, 1))
        with self.assertRaises(ValueError):
            self.loader.load_mesh_preview(self.path)

    def test_no_mesh_and_missing_source_produce_actionable_errors(self):
        self.path.write_bytes(mesh_file([], []))
        with self.assertRaisesRegex(ValueError, 'Mesh'):
            self.loader.load_mesh_preview(self.path)
        with self.assertRaisesRegex(ValueError, 'Vorschau'):
            self.loader.load_mesh_preview(self.path.with_name('missing.fbx'))


if __name__ == '__main__':
    unittest.main()
