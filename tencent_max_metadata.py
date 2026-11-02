"""Read or remove legacy importer metadata without loading a MAX scene.

Only Windows' documented OLE Structured Storage/property APIs are used. New
exports do not store importer metadata in MAX files. Cleanup removes only our
known old property from a staged MAX, preserving all unrelated data and scenes.
Reads deny concurrent writes, open no renderer, and require no extra packages.

References: Microsoft IPropertySetStorage/IPropertyStorage and StgOpenStorage;
Autodesk MAX Scene File Properties (the #custom set).
"""
import ctypes
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path


PROPERTY_NAME = 'TencentToVray.Manifest.v1'
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class MetadataError(ValueError):
    pass


class _GUID(ctypes.Structure):
    _fields_ = [('bytes', ctypes.c_ubyte * 16)]

    @classmethod
    def from_text(cls, value):
        return cls((ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le))


class _PropertyUnion(ctypes.Union):
    _fields_ = [('name', ctypes.c_wchar_p), ('propid', ctypes.c_uint32)]


class _PropSpec(ctypes.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('kind', ctypes.c_uint32), ('value', _PropertyUnion)]


class _VariantUnion(ctypes.Union):
    # PROPVARIANT has a 16-byte value union on 64-bit Windows. Including the
    # native pointer gives both the correct alignment and the LPWSTR member.
    _fields_ = [('text', ctypes.c_void_p), ('raw', ctypes.c_ubyte * 16)]


class _PropVariant(ctypes.Structure):
    _anonymous_ = ('value',)
    _fields_ = [('vt', ctypes.c_uint16), ('reserved1', ctypes.c_uint16),
                ('reserved2', ctypes.c_uint16), ('reserved3', ctypes.c_uint16),
                ('value', _VariantUnion)]


class _StatStorage(ctypes.Structure):
    _fields_ = [('name', ctypes.c_void_p), ('type', ctypes.c_uint32),
                ('size', ctypes.c_uint64), ('mtime', ctypes.c_uint64),
                ('ctime', ctypes.c_uint64), ('atime', ctypes.c_uint64),
                ('mode', ctypes.c_uint32), ('locks', ctypes.c_uint32),
                ('class_id', _GUID), ('state', ctypes.c_uint32),
                ('reserved', ctypes.c_uint32)]


class _StatProperty(ctypes.Structure):
    _fields_ = [('name', ctypes.c_void_p), ('propid', ctypes.c_uint32),
                ('vt', ctypes.c_uint16)]


_IID_PROPERTY_SET = _GUID.from_text('0000013a-0000-0000-c000-000000000046')
_FMTID_CUSTOM = _GUID.from_text('d5cdd505-2e9c-101b-9397-08002b2cf9ae')
_MISSING = 0x80030002
_READ = 0
_READWRITE = 2
_EXCLUSIVE = 0x10
_DENY_WRITE = 0x20
_TRANSACTED = 0x10000


def _check(result, operation):
    if result < 0:
        raise MetadataError('%s (Windows 0x%08X).' % (operation, result & 0xffffffff))
    return result


def _call(pointer, index, argtypes, *args):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])
    return method(pointer, *args)


def _release(pointer):
    if pointer:
        _call(pointer, 2, [])


def _check_property_stream_size(root):
    """Bound the property set before ReadMultiple can allocate its contents."""
    stream = ctypes.c_void_p()
    try:
        result = _call(root, 4, [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32,
                                ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)],
                       '\x05DocumentSummaryInformation', None, _READ | _EXCLUSIVE,
                       0, ctypes.byref(stream))
        if (result & 0xffffffff) == _MISSING:
            return
        _check(result, 'MAX-Eigenschaften können nicht geprüft werden')
        stat = _StatStorage()
        _check(_call(stream, 12, [ctypes.POINTER(_StatStorage), ctypes.c_uint32],
                     ctypes.byref(stat), 1), 'MAX-Eigenschaftsgröße ist nicht lesbar')
        if stat.size > MAX_MANIFEST_BYTES * 4:
            raise MetadataError('Benutzerdefinierte MAX-Eigenschaften sind zu groß.')
    finally:
        _release(stream)


@contextmanager
def _storage(path, write=False, _create_missing=False):
    if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise MetadataError('MAX-Metadaten benötigen 64-Bit-Windows.')
    path = Path(path).resolve()
    if not path.is_file():
        raise MetadataError('MAX-Datei fehlt: ' + str(path))
    # WinDLL returns raw HRESULTs so every failure follows our fail-closed
    # MetadataError path. OleDLL would raise OSError before _check can inspect it.
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    if initialized < 0 and (initialized & 0xffffffff) != 0x80010106:
        _check(initialized, 'Windows-Metadaten konnten nicht initialisiert werden')
    root = ctypes.c_void_p()
    property_set = ctypes.c_void_p()
    properties = ctypes.c_void_p()
    try:
        ole.StgOpenStorage.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(ctypes.c_void_p)]
        ole.StgOpenStorage.restype = ctypes.c_long
        mode = (_READWRITE | _EXCLUSIVE | _TRANSACTED) if write else (_READ | _DENY_WRITE)
        _check(ole.StgOpenStorage(str(path), None, mode, None, 0, ctypes.byref(root)),
               'MAX-Datei kann nicht sicher geöffnet werden')
        _check_property_stream_size(root)
        _check(_call(root, 0, [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(_IID_PROPERTY_SET), ctypes.byref(property_set)),
               'MAX-Dateieigenschaften sind nicht verfügbar')
        prop_mode = (_READWRITE if write else _READ) | _EXCLUSIVE
        result = _call(property_set, 4, [ctypes.POINTER(_GUID), ctypes.c_uint32,
                                        ctypes.POINTER(ctypes.c_void_p)],
                       ctypes.byref(_FMTID_CUSTOM), prop_mode, ctypes.byref(properties))
        if (result & 0xffffffff) == _MISSING:
            if not write or not _create_missing:
                yield ole, None
                return
            result = _call(property_set, 3, [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                            ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_void_p)],
                           ctypes.byref(_FMTID_CUSTOM), None, 0, prop_mode,
                           ctypes.byref(properties))
        _check(result, 'Benutzerdefinierte MAX-Eigenschaften können nicht geöffnet werden')
        yield ole, properties
        if write:
            _check(_call(properties, 9, [ctypes.c_uint32], 0),
                   'MAX-Eigenschaften konnten nicht gespeichert werden')
            _check(_call(root, 9, [ctypes.c_uint32], 0),
                   'MAX-Metadaten konnten nicht veröffentlicht werden')
    finally:
        _release(properties)
        _release(property_set)
        _release(root)
        if initialized >= 0:
            ole.CoUninitialize()


def _property_spec():
    result = _PropSpec()
    result.kind = 0  # PRSPEC_LPWSTR: identify our one property by name.
    result.name = PROPERTY_NAME
    return result


def _legacy_property_id(ole, properties):
    """Find the exact known property without reading/interpreting its value."""
    if not properties:
        return None
    enumerator = ctypes.c_void_p()
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    try:
        _check(_call(properties, 11, [ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(enumerator)), 'MAX-Eigenschaften können nicht aufgelistet werden')
        for _index in range(65536):
            entry, count = _StatProperty(), ctypes.c_uint32()
            try:
                result = _call(enumerator, 3, [ctypes.c_uint32,
                                              ctypes.POINTER(_StatProperty),
                                              ctypes.POINTER(ctypes.c_uint32)],
                               1, ctypes.byref(entry), ctypes.byref(count))
                _check(result, 'MAX-Eigenschaften können nicht gelesen werden')
                if not count.value:
                    return None
                if entry.name and ctypes.wstring_at(entry.name).casefold() == PROPERTY_NAME.casefold():
                    return entry.propid
            finally:
                if entry.name:
                    ole.CoTaskMemFree(entry.name)
        raise MetadataError('Zu viele benutzerdefinierte MAX-Eigenschaften.')
    finally:
        _release(enumerator)


def remove_manifest(max_path):
    """Remove only an inherited legacy importer property from a staged MAX.

    Returns True if the known property existed, False if no cleanup was needed.
    Does not add metadata or modify any live scene. Neither unrelated property
    values/names nor geometry streams are removed. Invalid/locked storage raises
    MetadataError; callers must then discard their unpublished staged file.
    """
    with _storage(max_path) as (ole, properties):
        if _legacy_property_id(ole, properties) is None:
            return False
    with _storage(max_path, write=True) as (ole, properties):
        identifier = _legacy_property_id(ole, properties)
        if identifier is None:
            raise MetadataError('MAX-Metadaten wurden während der Bereinigung geändert.')
        spec = _PropSpec()
        spec.kind, spec.propid = 1, identifier  # PRSPEC_PROPID
        _check(_call(properties, 5, [ctypes.c_uint32, ctypes.POINTER(_PropSpec)],
                     1, ctypes.byref(spec)), 'Alte Import-Metadaten können nicht entfernt werden')
        propid = ctypes.c_uint32(identifier)
        _check(_call(properties, 8, [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)],
                     1, ctypes.byref(propid)), 'Alter Import-Eigenschaftsname kann nicht entfernt werden')
    with _storage(max_path) as (ole, properties):
        if _legacy_property_id(ole, properties) is not None:
            raise MetadataError('Alte Import-Metadaten sind nach der Bereinigung noch vorhanden.')
    return True


def read_manifest(max_path):
    """Read native metadata without loading Max; return None when absent.

    Invalid storage, a wrong property type, malformed/oversized JSON, or a file
    currently being written raises MetadataError and must never authorize an
    overwrite. This method does not write or create any file.
    """
    spec, value = _property_spec(), _PropVariant()
    with _storage(max_path) as (ole, properties):
        if not properties:
            return None
        result = _call(properties, 3, [ctypes.c_uint32, ctypes.POINTER(_PropSpec),
                                      ctypes.POINTER(_PropVariant)],
                       1, ctypes.byref(spec), ctypes.byref(value))
        try:
            _check(result, 'Asset-Metadaten konnten nicht gelesen werden')
            if result == 1 or value.vt == 0:  # S_FALSE / VT_EMPTY means absent.
                return None
            if value.vt not in (30, 31) or not value.text:
                raise MetadataError('Asset-Metadaten haben ein ungültiges Textformat.')
            # ReadMultiple owns the allocation; bound the exposed string before
            # parsing. OLE validates the property stream's declared byte length.
            if value.vt == 30:
                raw = ctypes.string_at(value.text)
                if len(raw) > MAX_MANIFEST_BYTES:
                    raise MetadataError('Asset-Metadaten sind zu groß.')
                encoded = raw.decode('ascii')
            else:
                encoded = ctypes.wstring_at(value.text)
                if len(encoded) > MAX_MANIFEST_BYTES:
                    raise MetadataError('Asset-Metadaten sind zu groß.')
            try:
                def reject_nonfinite(value):
                    raise ValueError('Non-finite JSON value: ' + value)
                data = json.loads(encoded, parse_constant=reject_nonfinite)
            except (ValueError, RecursionError) as exc:
                raise MetadataError('Asset-Metadaten enthalten kein gültiges JSON.') from exc
            if not isinstance(data, dict):
                raise MetadataError('Asset-Metadaten sind kein Objekt.')
            return data
        except UnicodeError as exc:
            raise MetadataError('Asset-Metadaten haben eine ungültige Zeichenkodierung.') from exc
        finally:
            ole.PropVariantClear.argtypes = [ctypes.POINTER(_PropVariant)]
            ole.PropVariantClear.restype = ctypes.c_long
            ole.PropVariantClear(ctypes.byref(value))
