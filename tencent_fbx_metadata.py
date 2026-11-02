"""Read FBX Video/Texture/Material records by ownership, never by byte proximity.

Geometry arrays are skipped; no FBX SDK or image library is needed for extraction.
Only embedded media and explicitly referenced files beside the FBX are eligible.
"""
from dataclasses import dataclass, field
from pathlib import Path
import struct


@dataclass
class Record:
    name: str
    props: list = field(default_factory=list)
    children: list = field(default_factory=list)

    def child(self, name):
        return next((c for c in self.children if c.name.lower() == name.lower()), None)

    def value(self, name, default=None):
        child = self.child(name)
        return child.props[0] if child and child.props else default


def object_name(value):
    return str(value).split('\x00')[0].split('::')[-1]


def read_records(path):
    data = Path(path).read_bytes()
    if not data.startswith(b'Kaydara FBX Binary  \x00\x1a\x00'):
        raise ValueError('Nur binäre FBX-Dateien werden unterstützt. Bitte als binäre FBX exportieren.')
    version = struct.unpack_from('<I', data, 23)[0]
    header, size = ('<QQQB', 25) if version >= 7500 else ('<IIIB', 13)

    def prop(pos):
        tag = chr(data[pos]); pos += 1
        scalar = {'Y': ('<h', 2), 'C': ('<?', 1), 'I': ('<i', 4),
                  'F': ('<f', 4), 'D': ('<d', 8), 'L': ('<q', 8)}
        if tag in scalar:
            fmt, count = scalar[tag]
            return struct.unpack_from(fmt, data, pos)[0], pos + count
        if tag in 'SR':
            count = struct.unpack_from('<I', data, pos)[0]; pos += 4
            value = data[pos:pos + count]
            if len(value) != count:
                raise ValueError('Unvollständige FBX-Eigenschaft.')
            return value.decode('utf-8', 'replace') if tag == 'S' else value, pos + count
        if tag in 'fdlibc':
            count, encoding, length = struct.unpack_from('<III', data, pos)
            return None, pos + 12 + length
        raise ValueError('Unbekannter FBX-Eigenschaftstyp: ' + tag)

    def records(pos, boundary, depth=0):
        if depth > 64:
            raise ValueError('FBX-Verschachtelung zu tief.')
        result = []
        while pos + size <= boundary:
            end, count, length, name_len = struct.unpack_from(header, data, pos)
            if not end:
                break
            if end <= pos or end > boundary:
                raise ValueError('Ungültige FBX-Record-Grenzen.')
            start = pos + size
            name = data[start:start + name_len].decode('utf-8', 'replace')
            p = start + name_len
            if name in {'Geometry', 'AnimationCurve', 'AnimationCurveNode', 'Deformer', 'Pose'}:
                pos = end
                continue
            values = []
            for _ in range(count):
                value, p = prop(p)
                values.append(value)
            if p != start + name_len + length:
                raise ValueError('Ungültige FBX-Eigenschaftslänge.')
            result.append(Record(name, values, records(p, end, depth + 1)))
            pos = end
        return result

    return records(27, len(data))


def inspect(path):
    records = read_records(path)
    objects = next((r for r in records if r.name == 'Objects'), Record('Objects'))
    connections = next((r for r in records if r.name == 'Connections'), Record('Connections'))
    by_id = {r.props[0]: r for r in objects.children if r.props}
    links = [r.props for r in connections.children if r.name == 'C' and len(r.props) >= 3]
    return by_id, links
