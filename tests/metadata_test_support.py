"""TEST ONLY: create synthetic legacy-property fixtures; not used by the importer."""
import ctypes
import json
import tencent_max_metadata as metadata
from tencent_max_metadata import MetadataError, _PropVariant, _PropSpec, _property_spec, _check, _call

def write_manifest(max_path, manifest):
    """Set one native Custom File Property in an unpublished staged MAX file.

    No sidecar, alternate data stream, global setting, scene or process change.
    Any error must abort publication of the staged file. Existing user custom
    properties are retained. Ownership/path validation belongs to asset_store.
    """
    if not isinstance(manifest, dict):
        raise MetadataError('Asset-Metadaten müssen ein Objekt sein.')
    try:
        encoded = json.dumps(manifest, ensure_ascii=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise MetadataError('Asset-Metadaten sind nicht als JSON speicherbar.') from exc
    if len(encoded) > metadata.MAX_MANIFEST_BYTES:
        raise MetadataError('Asset-Metadaten sind zu groß.')
    if json.loads(encoded) != manifest:
        raise MetadataError('Asset-Metadaten enthalten nicht unterstützte Datentypen.')
    # An ASCII JSON string is lossless for Unicode (JSON escapes), and native
    # Max uses VT_LPSTR for its Text custom properties.
    buffer = ctypes.create_string_buffer(encoded.encode('ascii'))
    value = _PropVariant()
    value.vt = 30  # VT_LPSTR
    value.text = ctypes.cast(buffer, ctypes.c_void_p).value
    spec = _property_spec()
    with metadata._storage(max_path, write=True, _create_missing=True) as (_ole, properties):
        _check(_call(properties, 4, [ctypes.c_uint32, ctypes.POINTER(_PropSpec),
                                    ctypes.POINTER(_PropVariant), ctypes.c_uint32],
                     1, ctypes.byref(spec), ctypes.byref(value), 2),
               'Asset-Metadaten konnten nicht geschrieben werden')
    if metadata.read_manifest(max_path) != manifest:
        raise MetadataError('Gespeicherte Asset-Metadaten sind nicht vollständig lesbar.')

