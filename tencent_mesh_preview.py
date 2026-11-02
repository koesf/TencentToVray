"""Small, read-only static FBX mesh previews; no SDK, Qt, or scene operations.

Binary FBX arrays are read on demand from an mmap. Embedded images, animation
curves, normals and UVs are skipped by record boundaries. The returned mesh is
only a naming aid: vertex clustering simplifies the surface, materials and
deformations are deliberately not evaluated. Polygon faces use fan
triangulation; concave n-gons can therefore differ from the final FBX import.
"""
from array import array
from dataclasses import dataclass
import math
import mmap
from pathlib import Path
import struct
import sys
import zlib


@dataclass(frozen=True)
class MeshPreview:
    vertices: tuple
    triangles: tuple
    mesh_count: int
    source_vertex_count: int
    source_triangle_count: int
    model_names: tuple
    warnings: tuple


class PreviewCancelled(Exception):
    """The caller no longer needs this read-only preview."""


_MAGIC = b'Kaydara FBX Binary  \x00\x1a\x00'
_MAX_ARRAY_BYTES = 128 * 1024 * 1024
_MAX_GEOMETRIES = 512
_MAX_OBJECTS = 50000
_MAX_SOURCE_VERTICES = 12000000
_MAX_SOURCE_INDICES = 48000000
_IDENTITY = (1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.)


def _check(cancelled):
    if cancelled is not None and cancelled():
        raise PreviewCancelled('Mesh-Vorschau abgebrochen.')


@dataclass(frozen=True)
class _ArrayRef:
    tag: str
    count: int
    encoding: int
    start: int
    length: int


@dataclass(frozen=True)
class _Node:
    name: str
    count: int
    start: int
    props_end: int
    end: int


class _Reader:
    def __init__(self, data, cancelled):
        self.data = data
        self.cancelled = cancelled
        self.record_count = 0
        if len(data) < 27 or data[:23] != _MAGIC:
            raise ValueError('Die Mesh-Vorschau benötigt eine binäre FBX-Datei.')
        self.version = struct.unpack_from('<I', data, 23)[0]
        if not 7000 <= self.version < 8000:
            raise ValueError('Diese FBX-Version wird für die Mesh-Vorschau nicht unterstützt.')
        self.fmt, self.size = ('<QQQB', 25) if self.version >= 7500 else ('<IIIB', 13)

    def nodes(self, start, boundary):
        pos = start
        while pos < boundary:
            _check(self.cancelled)
            if pos + self.size > boundary:
                raise ValueError('Unvollständiger FBX-Datensatz.')
            end, count, length, name_len = struct.unpack_from(self.fmt, self.data, pos)
            if end == 0:
                if any(self.data[pos:pos + self.size]):
                    raise ValueError('Ungültiger FBX-Abschluss.')
                return
            prop_start = pos + self.size + name_len
            prop_end = prop_start + length
            if not pos < end <= boundary or prop_end > end or name_len == 0 or count > 4096:
                raise ValueError('Ungültige FBX-Datensatzgrenzen.')
            self.record_count += 1
            if self.record_count > 500000:
                raise ValueError('Zu viele FBX-Datensätze für die Mesh-Vorschau.')
            name = self.data[pos + self.size:prop_start].decode('utf-8', 'replace')
            yield _Node(name, count, prop_start, prop_end, end)
            pos = end

    def children(self, node):
        return self.nodes(node.props_end, node.end)

    def props(self, node):
        result = []
        p, end = node.start, node.props_end
        scalar = {'Y': ('<h', 2), 'C': ('<?', 1), 'I': ('<i', 4),
                  'F': ('<f', 4), 'D': ('<d', 8), 'L': ('<q', 8)}
        for _ in range(node.count):
            if p >= end:
                raise ValueError('Unvollständige FBX-Eigenschaft.')
            tag = chr(self.data[p])
            p += 1
            if tag in scalar:
                fmt, length = scalar[tag]
                if p + length > end:
                    raise ValueError('Unvollständige FBX-Zahl.')
                value = struct.unpack_from(fmt, self.data, p)[0]
                p += length
            elif tag in 'SR':
                if p + 4 > end:
                    raise ValueError('Unvollständiger FBX-Text.')
                length = struct.unpack_from('<I', self.data, p)[0]
                p += 4
                if p + length > end:
                    raise ValueError('Ungültige FBX-Textlänge.')
                if tag == 'S':
                    if length > 65536:
                        raise ValueError('FBX-Text zu lang für die Mesh-Vorschau.')
                    value = self.data[p:p + length].decode('utf-8', 'replace')
                else:
                    value = None
                p += length
            elif tag in 'fdlibc':
                if p + 12 > end:
                    raise ValueError('Unvollständige FBX-Geometriedaten.')
                count, encoding, length = struct.unpack_from('<III', self.data, p)
                p += 12
                if p + length > end or encoding not in (0, 1):
                    raise ValueError('Ungültige FBX-Arraygrenzen oder Komprimierung.')
                value = _ArrayRef(tag, count, encoding, p, length)
                p += length
            else:
                raise ValueError('Unbekannter FBX-Eigenschaftstyp: ' + tag)
            result.append(value)
        if p != end:
            raise ValueError('Ungültige FBX-Eigenschaftslänge.')
        return result

    def read_array(self, ref, allowed):
        if not isinstance(ref, _ArrayRef) or ref.tag not in allowed:
            raise ValueError('Ungültiger Datentyp der FBX-Geometrie.')
        expected = ref.count * {'d': 8, 'f': 4, 'i': 4, 'l': 8}[ref.tag]
        if expected > _MAX_ARRAY_BYTES or ref.length > _MAX_ARRAY_BYTES:
            raise ValueError('FBX-Geometrie zu groß für die Mesh-Vorschau.')
        # array('l') is platform-sized (32-bit even in 64-bit Windows Python).
        # FBX 'l' is always a signed 64-bit integer.
        result = array('q' if ref.tag == 'l' else ref.tag)
        if ref.encoding == 0:
            if ref.length != expected:
                raise ValueError('FBX-Geometrielänge stimmt nicht mit der Punktzahl überein.')
            for start in range(ref.start, ref.start + ref.length, 1024 * 1024):
                _check(self.cancelled)
                result.frombytes(self.data[start:min(start + 1024 * 1024, ref.start + ref.length)])
        else:
            inflater = zlib.decompressobj()
            raw = bytearray()
            for start in range(ref.start, ref.start + ref.length, 1024 * 1024):
                _check(self.cancelled)
                raw.extend(inflater.decompress(
                    self.data[start:min(start + 1024 * 1024, ref.start + ref.length)],
                    expected + 1 - len(raw)))
                if len(raw) > expected or inflater.unconsumed_tail:
                    raise ValueError('Komprimierte FBX-Geometrie überschreitet ihre angegebene Größe.')
                if inflater.unused_data:
                    raise ValueError('Unerwartete Daten nach komprimierter FBX-Geometrie.')
            if not inflater.eof or len(raw) != expected:
                raise ValueError('Unvollständige komprimierte FBX-Geometrie.')
            result.frombytes(raw)
        if sys.byteorder != 'little':
            result.byteswap()
        return result


def _properties(reader, node):
    result = {}
    for child in reader.children(node):
        if child.name not in ('Properties70', 'Properties60'):
            continue
        for record in reader.children(child):
            if record.name not in ('P', 'Property'):
                continue
            values = reader.props(record)
            if values and isinstance(values[0], str):
                # Properties70 has four header values; Properties60 has three.
                result[values[0]] = values[4 if record.name == 'P' else 3:]
    return result


def _vector(props, name, default=(0., 0., 0.)):
    raw = props.get(name, default)
    if len(raw) != 3:
        raise ValueError('Ungültige FBX-Transformation: ' + name)
    try:
        value = tuple(float(v) for v in raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('Ungültige FBX-Transformation: ' + name) from exc
    if not all(math.isfinite(v) and abs(v) < 1e100 for v in value):
        raise ValueError('Ungültige FBX-Koordinaten: ' + name)
    return value


def _number(props, name, default):
    raw = props.get(name)
    value = raw[-1] if raw else default
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Ungültige FBX-Einstellung: ' + name)
    return value


def _mul(a, b):
    return tuple(sum(a[r * 4 + k] * b[k * 4 + c] for k in range(4))
                 for r in range(4) for c in range(4))


def _translation(v):
    return (1., 0., 0., v[0], 0., 1., 0., v[1], 0., 0., 1., v[2], 0., 0., 0., 1.)


def _scale(v):
    return (v[0], 0., 0., 0., 0., v[1], 0., 0., 0., 0., v[2], 0., 0., 0., 0., 1.)


def _rotation(v, order=0):
    if order not in range(6):
        raise ValueError('Sphärische FBX-Rotationen werden in der Mesh-Vorschau nicht unterstützt.')
    result = _IDENTITY
    for axis in ('XYZ', 'XZY', 'YZX', 'YXZ', 'ZXY', 'ZYX')[order]:
        angle = math.radians(v['XYZ'.index(axis)])
        c, s = math.cos(angle), math.sin(angle)
        if axis == 'X':
            matrix = (1., 0., 0., 0., 0., c, -s, 0., 0., s, c, 0., 0., 0., 0., 1.)
        elif axis == 'Y':
            matrix = (c, 0., s, 0., 0., 1., 0., 0., -s, 0., c, 0., 0., 0., 0., 1.)
        else:
            matrix = (c, -s, 0., 0., s, c, 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.)
        result = _mul(matrix, result)
    return result


def _local(props):
    # Autodesk's FBX node formula includes offsets, pre/post rotation and pivots:
    # https://help.autodesk.com/cloudhelp/2018/ENU/FBX-Developer-Help/nodes_and_scene_graph/fbx_nodes/computing_transformation_matrix.html
    rp, sp = _vector(props, 'RotationPivot'), _vector(props, 'ScalingPivot')
    post = _rotation(_vector(props, 'PostRotation'))
    post_inverse = tuple(post[c * 4 + r] for r in range(4) for c in range(4))
    chain = (_translation(_vector(props, 'Lcl Translation')),
             _translation(_vector(props, 'RotationOffset')), _translation(rp),
             _rotation(_vector(props, 'PreRotation')),
             _rotation(_vector(props, 'Lcl Rotation'), int(_number(props, 'RotationOrder', 0))),
             post_inverse, _translation(tuple(-v for v in rp)),
             _translation(_vector(props, 'ScalingOffset')), _translation(sp),
             _scale(_vector(props, 'Lcl Scaling', (1., 1., 1.))),
             _translation(tuple(-v for v in sp)))
    result = _IDENTITY
    for matrix in chain:
        result = _mul(result, matrix)
    return result


def _geometric(props):
    # Geometric TRS belongs only to this model's geometry, never its children.
    return _mul(_mul(_translation(_vector(props, 'GeometricTranslation')),
                     _rotation(_vector(props, 'GeometricRotation'))),
                _scale(_vector(props, 'GeometricScaling', (1., 1., 1.))))


def _axis(props):
    # FBX defaults to Maya Y-up; target basis is the Max convention: +X, -Y front, +Z up.
    axes = [int(_number(props, name, default)) for name, default in
            (('CoordAxis', 0), ('FrontAxis', 2), ('UpAxis', 1))]
    signs = [int(_number(props, name, 1)) for name in
             ('CoordAxisSign', 'FrontAxisSign', 'UpAxisSign')]
    if sorted(axes) != [0, 1, 2] or any(v not in (-1, 1) for v in signs):
        raise ValueError('Ungültiges FBX-Achsensystem.')
    result = [0.] * 16
    for row, col in enumerate(axes):
        result[row * 4 + col] = signs[row] * (-1 if row == 1 else 1)
    result[15] = 1.
    return tuple(result)


def _name(raw):
    return str(raw).split('\x00')[0].split('::')[-1].strip()


def _read_scene(reader):
    geometry, models, connections, settings, warnings = {}, {}, [], {}, []
    objects = None
    for node in reader.nodes(27, len(reader.data)):
        if node.name == 'GlobalSettings':
            settings = _properties(reader, node)
        elif node.name == 'Objects':
            objects = node
        elif node.name == 'Connections':
            for link in reader.children(node):
                if link.name == 'C':
                    values = reader.props(link)
                    if len(values) >= 3 and values[0] == 'OO':
                        connections.append((values[1], values[2]))
                        if len(connections) > _MAX_OBJECTS * 2:
                            raise ValueError('Zu viele FBX-Verknüpfungen für die Mesh-Vorschau.')
    if objects is None:
        raise ValueError('Die FBX-Datei enthält keine Mesh-Objekte.')
    deforming = False
    for count, node in enumerate(reader.children(objects)):
        if count >= _MAX_OBJECTS:
            raise ValueError('Zu viele FBX-Objekte für die Mesh-Vorschau.')
        if node.name in ('Deformer', 'AnimationCurve', 'AnimationCurveNode'):
            deforming = True
        if node.name not in ('Geometry', 'Model'):
            continue
        values = reader.props(node)
        if len(values) < 3 or not isinstance(values[0], int):
            raise ValueError('Ungültige FBX-Objektkennung.')
        ident, name, kind = values[:3]
        if node.name == 'Model':
            models[ident] = (_name(name), _properties(reader, node))
        elif kind == 'Mesh':
            arrays = {}
            for child in reader.children(node):
                if child.name in ('Vertices', 'PolygonVertexIndex'):
                    values = reader.props(child)
                    if len(values) != 1 or not isinstance(values[0], _ArrayRef):
                        raise ValueError('Fehlende FBX-Meshdaten: ' + child.name)
                    arrays[child.name] = values[0]
            if len(arrays) != 2:
                raise ValueError('FBX-Mesh ohne Punkte oder Flächen.')
            if arrays['Vertices'].count % 3:
                raise ValueError('Unvollständige FBX-Punktkoordinaten.')
            geometry[ident] = (_name(name), arrays['Vertices'], arrays['PolygonVertexIndex'])
            if len(geometry) > _MAX_GEOMETRIES:
                raise ValueError('Zu viele Meshes für die Mesh-Vorschau.')
    if deforming:
        warnings.append('Statische Vorschau ohne Animation, Skinning oder Formänderungen.')
    parents, owners = {}, {}
    for child, parent in connections:
        if child in geometry and parent in models:
            owners.setdefault(child, []).append(parent)
        elif child in models and parent in models:
            if child in parents and parents[child] != parent:
                raise ValueError('FBX-Modell mit mehreren übergeordneten Modellen.')
            parents[child] = parent
    worlds = {}

    def world(ident, chain=()):
        if ident in worlds:
            return worlds[ident]
        if ident in chain or len(chain) >= 64:
            raise ValueError('Zyklische oder zu tiefe FBX-Modellhierarchie.')
        props = models[ident][1]
        parent = parents.get(ident)
        result = _local(props)
        if parent is not None:
            result = _mul(world(parent, chain + (ident,)), result)
            if int(_number(props, 'InheritType', 0)) != 1:
                pscale = _vector(models[parent][1], 'Lcl Scaling', (1., 1., 1.))
                if max(pscale) - min(pscale) > 1e-8 or int(_number(props, 'InheritType', 0)) == 2:
                    warnings.append('Spezielle Skalierungsvererbung wird in der Vorschau angenähert.')
        worlds[ident] = result
        return result

    instances = []
    basis = _axis(settings)
    for ident, (name, vertices, indices) in geometry.items():
        model_ids = list(dict.fromkeys(owners.get(ident, ())))
        for model in model_ids or [None]:
            transform = (_mul(world(model), _geometric(models[model][1]))
                         if model is not None else _IDENTITY)
            instances.append((models[model][0] if model is not None else name,
                              vertices, indices, _mul(basis, transform)))
    if not instances:
        raise ValueError('Die FBX-Datei enthält kein darstellbares Mesh.')
    if len(instances) > _MAX_GEOMETRIES:
        raise ValueError('Zu viele Mesh-Instanzen für die Vorschau.')
    if (sum(v.count // 3 for _, v, _, _ in instances) > _MAX_SOURCE_VERTICES or
            sum(i.count for _, _, i, _ in instances) > _MAX_SOURCE_INDICES):
        raise ValueError('Die gesamte FBX-Geometrie ist zu groß für die Mesh-Vorschau.')
    return instances, warnings


def _faces(indices, vertex_count, cancelled):
    first, previous, length = 0, 0, 0
    for offset, raw in enumerate(indices):
        if offset % 32768 == 0:
            _check(cancelled)
        last = raw < 0
        index = -raw - 1 if last else raw
        if not 0 <= index < vertex_count:
            raise ValueError('FBX-Fläche verweist auf einen ungültigen Punkt.')
        if length == 0:
            first = index
        elif length >= 2:
            yield first, previous, index
        previous = index
        length += 1
        if last:
            if length < 3:
                raise ValueError('FBX-Fläche mit weniger als drei Punkten.')
            length = 0
    if length:
        raise ValueError('Unvollständige FBX-Fläche ohne Abschlussindex.')


def _transformed(raw, matrix, cancelled):
    result = array('d')
    lx = ly = lz = math.inf
    hx = hy = hz = -math.inf
    a, b, c, d, e, f, g, h, i, j, k, l = matrix[:12]
    for n in range(0, len(raw), 3):
        if n % 98304 == 0:
            _check(cancelled)
        x, y, z = raw[n], raw[n + 1], raw[n + 2]
        tx, ty, tz = (a * x + b * y + c * z + d,
                      e * x + f * y + g * z + h,
                      i * x + j * y + k * z + l)
        if not (abs(tx) <= 1e100 and abs(ty) <= 1e100 and abs(tz) <= 1e100):
            raise ValueError('FBX-Mesh enthält ungültige Punktkoordinaten.')
        if tx < lx: lx = tx
        if ty < ly: ly = ty
        if tz < lz: lz = tz
        if tx > hx: hx = tx
        if ty > hy: hy = ty
        if tz > hz: hz = tz
        result.extend((tx, ty, tz))
    return result, (lx, ly, lz), (hx, hy, hz)


def _clusters(points, low, high, resolution, vertex_limit, cancelled):
    extent = max(high[n] - low[n] for n in range(3))
    factor = resolution / extent if extent > 0 else 1.
    cells, sums, remap = {}, [], array('I')
    for offset in range(0, len(points), 3):
        if offset % 98304 == 0:
            _check(cancelled)
        x, y, z = points[offset], points[offset + 1], points[offset + 2]
        key = (int((x - low[0]) * factor), int((y - low[1]) * factor),
               int((z - low[2]) * factor))
        index = cells.get(key)
        if index is None:
            index = len(sums)
            if index >= vertex_limit:
                return None, None
            cells[key] = index
            sums.append([x, y, z, 1])
        else:
            total = sums[index]
            total[0] += x
            total[1] += y
            total[2] += z
            total[3] += 1
        remap.append(index)
    return [(x / count, y / count, z / count) for x, y, z, count in sums], remap


def _simplify(points, indices, low, high, triangle_budget, vertex_budget, cancelled):
    count = len(points) // 3
    # Validate once and keep compact uint32 indices. Python tuple-per-face lists
    # would cost hundreds of MB for generated meshes; packed indices stay small.
    packed = array('I')
    for face in _faces(indices, count, cancelled):
        if len(packed) * 4 >= _MAX_ARRAY_BYTES - 12:
            raise ValueError('Zu viele Flächen für die Mesh-Vorschau.')
        packed.extend(face)
    source_triangles = len(packed) // 3

    def faces():
        values = iter(packed)
        for number, face in enumerate(zip(values, values, values)):
            if number % 32768 == 0:
                _check(cancelled)
            yield face

    if not source_triangles:
        return [], [], 0, False
    if source_triangles <= triangle_budget and count <= vertex_budget:
        return ([tuple(points[n:n + 3]) for n in range(0, len(points), 3)],
                list(faces()), source_triangles, False)
    resolution = max(2., math.sqrt(triangle_budget / 8.))
    best = None
    for attempt in range(8):
        vertices, remap = _clusters(points, low, high, resolution,
                                    max(vertex_budget * 2, 16), cancelled)
        if vertices is None or len(vertices) > vertex_budget:
            resolution *= .72
            continue
        triangles, unique = [], set()
        too_many = False
        for a, b, c in faces():
            a, b, c = remap[a], remap[b], remap[c]
            if a == b or b == c or a == c:
                continue
            key = tuple(sorted((a, b, c)))
            if key in unique:
                continue
            unique.add(key)
            triangles.append((a, b, c))
            if len(triangles) > max(triangle_budget * 4, 64):
                too_many = True
                break
        if triangles and not too_many:
            best = vertices, triangles
        if triangles and len(triangles) <= triangle_budget and not too_many:
            return vertices, triangles, source_triangles, False
        resolution *= (min(.85, math.sqrt(triangle_budget / len(triangles)) * .94)
                       if triangles and not too_many else .72)
    if best is None:
        # Degenerate/small disconnected pieces can collapse into one cell. Keep
        # evenly distributed original triangles as a strictly bounded fallback.
        vertices, triangles, mapping = [], [], {}
        step = max(1, math.ceil(source_triangles / max(1, min(triangle_budget, vertex_budget // 3))))
        for number, face in enumerate(faces()):
            if number % step:
                continue
            if len(vertices) + sum(v not in mapping for v in face) > vertex_budget:
                break
            output = []
            for index in face:
                if index not in mapping:
                    mapping[index] = len(vertices)
                    vertices.append(tuple(points[index * 3:index * 3 + 3]))
                output.append(mapping[index])
            triangles.append(tuple(output))
            if len(triangles) >= triangle_budget:
                break
        return vertices, triangles, source_triangles, True
    vertices, triangles = best
    step = len(triangles) / triangle_budget
    return vertices, [triangles[int(n * step)] for n in range(triangle_budget)], source_triangles, True


def _budgets(weights, total, minimum):
    if total < minimum * len(weights):
        raise ValueError('Das Vorschau-Limit ist für die Anzahl der Meshes zu klein.')
    spare, sum_weights = total - minimum * len(weights), sum(weights)
    budgets = [minimum + int(spare * weight / sum_weights) for weight in weights]
    for n in range(total - sum(budgets)):
        budgets[n % len(budgets)] += 1
    return budgets


def load_mesh_preview(path, max_triangles=18000, max_vertices=20000, cancelled=None):
    """Read a binary FBX as a bounded, normalized, Z-up static preview.

    The callback is checked during reading and simplification. Values returned
    here must never be used as import geometry: this is a simplified naming aid.
    Malformed/unsupported input raises a German ValueError; cancellation raises
    PreviewCancelled. No files are created or changed.
    """
    if (not isinstance(max_triangles, int) or not 1 <= max_triangles <= 100000 or
            not isinstance(max_vertices, int) or not 3 <= max_vertices <= 100000):
        raise ValueError('Ungültige Größenbegrenzung für die Mesh-Vorschau.')
    _check(cancelled)
    try:
        with Path(path).open('rb') as handle:
            if handle.seek(0, 2) < 27:
                raise ValueError('Die FBX-Datei ist leer oder unvollständig.')
            handle.seek(0)
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                reader = _Reader(data, cancelled)
                instances, warnings = _read_scene(reader)
                weights = [max(1, indices.count) for _, _, indices, _ in instances]
                tri_budgets = _budgets(weights, max_triangles, 1)
                vert_budgets = _budgets(weights, max_vertices, 3)
                all_vertices, all_triangles, names = [], [], []
                source_vertices, source_triangles = 0, 0
                for number, (name, vertex_ref, index_ref, matrix) in enumerate(instances):
                    _check(cancelled)
                    raw = reader.read_array(vertex_ref, 'df')
                    indices = reader.read_array(index_ref, 'il')
                    if not raw or not indices:
                        raise ValueError('FBX-Mesh enthält keine Punkte oder Flächen.')
                    points, low, high = _transformed(raw, matrix, cancelled)
                    del raw
                    vertices, triangles, face_count, sampled = _simplify(
                        points, indices, low, high, tri_budgets[number], vert_budgets[number], cancelled)
                    source_vertices += len(points) // 3
                    source_triangles += face_count
                    del points, indices
                    offset = len(all_vertices)
                    all_vertices.extend(vertices)
                    all_triangles.extend((a + offset, b + offset, c + offset) for a, b, c in triangles)
                    if name and name not in names:
                        names.append(name)
                    if sampled:
                        warnings.append('Sehr dichte Geometrie wird in der Vorschau zusätzlich ausgedünnt.')
                if not all_vertices or not all_triangles:
                    raise ValueError('Die FBX-Datei enthält keine darstellbaren Flächen.')
                low = [min(v[n] for v in all_vertices) for n in range(3)]
                high = [max(v[n] for v in all_vertices) for n in range(3)]
                extent = max(high[n] - low[n] for n in range(3))
                if extent <= 1e-100:
                    raise ValueError('Das FBX-Mesh hat keine darstellbare Ausdehnung.')
                center = [(low[n] + high[n]) * .5 for n in range(3)]
                factor = 2. / extent
                vertices = tuple(tuple((v[n] - center[n]) * factor for n in range(3)) for v in all_vertices)
                _check(cancelled)
                return MeshPreview(vertices, tuple(all_triangles), len(instances), source_vertices,
                                   source_triangles, tuple(names), tuple(dict.fromkeys(warnings)))
    except PreviewCancelled:
        raise
    except ValueError:
        raise
    except (OSError, struct.error, zlib.error, OverflowError, TypeError, KeyError) as exc:
        raise ValueError('Mesh-Vorschau konnte die FBX-Datei nicht lesen: ' + str(exc)) from exc
