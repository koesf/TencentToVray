"""Validated asset discovery and cooperative, rollback-safe file publication.

This module does not import pymxs. A transaction holds an operating-system lock
for the whole import, stages on the destination volume, and checks the exact
revision the user approved before replacing any existing files. Unreferenced old
maps are deliberately preserved: other MAX scenes can still refer to them.
"""
import hashlib
import errno
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path


class AssetStoreError(RuntimeError):
    pass


class AssetBusyError(AssetStoreError):
    pass


class AssetChangedError(AssetStoreError):
    pass


class AssetRollbackError(AssetStoreError):
    pass


def _uncertain_io_error(error):
    """A transport/storage failure can arrive after a remote write committed."""
    if not isinstance(error, OSError):
        return False
    windows_errors = {53, 54, 55, 58, 59, 64, 67, 121, 995, 1117,
                      1201, 1225, 1229, 1231, 1232, 1233, 1234, 1236, 2250}
    io_errors = {getattr(errno, name, None) for name in (
        'EIO', 'ENETDOWN', 'ENETUNREACH', 'ENETRESET', 'ECONNABORTED',
        'ECONNRESET', 'ETIMEDOUT', 'EHOSTDOWN', 'EHOSTUNREACH', 'ESTALE')}
    return (getattr(error, 'winerror', None) in windows_errors
            or (error.errno is not None and error.errno in io_errors))


def _inside(path, folder):
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


def _path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _fingerprint(paths):
    """Hash contents as well as timestamps; same-size edits must be noticed."""
    records = []
    for path in sorted({Path(p).resolve() for p in paths}, key=_path_key):
        if not path.exists():
            records.append((_path_key(path), None))
            continue
        if not path.is_file():
            raise AssetStoreError('Asset-Datei ist kein regulärer Dateipfad: ' + str(path))
        before = path.stat()
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise AssetChangedError('Asset wurde während der Prüfung geändert. Bitte erneut auswählen.')
        records.append((_path_key(path), after.st_size, after.st_mtime_ns, digest.hexdigest()))
    return hashlib.sha256(json.dumps(records, ensure_ascii=False).encode('utf-8')).hexdigest()


def _read_candidate(core, root, base, maps_root, manifest_path):
    if _is_link(manifest_path) or _is_link(manifest_path.parent) or _is_link(maps_root):
        raise ValueError('Asset-Begleitdatei und Map-Ordner dürfen keine Verknüpfungen sein.')
    manifest_path = manifest_path.resolve()
    def read_data():
        if manifest_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Asset-Manifest ist zu groß.')
        return json.loads(manifest_path.read_text(encoding='utf-8'))
    data = read_data()
    if not isinstance(data, dict):
        raise ValueError('Ungültiges Asset-Manifest.')
    requested = data.get('requested_asset_name', '')
    if (not isinstance(requested, str)
            or core.prefixed_asset_name(core.validate_asset_name(requested)).casefold()
            != core.prefixed_asset_name(base).casefold()):
        return None
    if core.validate_asset_name(requested) != requested:
        raise ValueError('Ungültiger Asset-Name im Manifest.')
    asset_id = data.get('asset_id', '')
    short_code = data.get('asset_short_code', '')
    time_code = data.get('import_time_code', '')
    asset = data.get('asset', '')
    code = data.get('asset_code', '')
    if not isinstance(asset_id, str) or not re.fullmatch(r'[0-9a-f]{32}', asset_id):
        raise ValueError('Ungültige Asset-ID.')
    if not isinstance(short_code, str) or (short_code and not re.fullmatch(r'[A-HJ-NP-Z2-9]{8}', short_code)):
        raise ValueError('Ungültige Asset-Kurzkennung.')
    # Keep existing 2.02/2.03 filenames stable for relinking and replacement.
    file_identity = short_code or asset_id
    if not isinstance(time_code, str) or not re.fullmatch(r'[0-9]{4}', time_code):
        raise ValueError('Ungültige Importzeit.')
    if asset != requested + '_' + time_code + '_' + file_identity:
        raise ValueError('Asset-Name und ID passen nicht zusammen.')
    code_start = core.prefixed_asset_name(asset) + '_'
    if not isinstance(code, str) or not code.startswith(code_start):
        raise ValueError('Ungültiger Asset-Code.')
    prefix = code[len(code_start):]
    if not re.fullmatch(r'[A-Za-z0-9]+', prefix) or core.asset_code(prefix, asset) != code:
        raise ValueError('Ungültiger Datums-Code.')
    expected_folder = maps_root / (time_code + '_' + file_identity)
    folder = manifest_path.parent
    if _path_key(folder) != _path_key(expected_folder) or not _inside(folder, root):
        raise ValueError('Asset-Manifest liegt außerhalb seines Asset-Ordners.')
    max_path = root / (code + '.max')
    if _is_link(max_path):
        raise ValueError('Eine MAX-Verknüpfung ist kein verwaltetes Asset.')
    max_file = max_path.resolve()
    saved_max = data.get('max_file')
    if saved_max:
        # The old absolute directory may have moved. Only the owned basename is
        # reusable, and the current destination is always derived from the code.
        basename = str(saved_max).replace('\\', '/').rsplit('/', 1)[-1]
        if basename.casefold() != max_file.name.casefold():
            raise ValueError('MAX-Dateiname und Asset-Code passen nicht zusammen.')
    managed = {manifest_path, folder / 'import_report.txt', max_file}
    def checked_map_path(relative=None, absolute=None):
        if relative:
            relative = Path(relative)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Ungültiger relativer Map-Pfad.')
            target = (root / relative).resolve()
        elif absolute:
            target = Path(absolute).resolve()
        else:
            raise ValueError('Map-Pfad fehlt.')
        if not _inside(target, folder) or target == folder:
            raise ValueError('Map-Pfad liegt außerhalb des Asset-Ordners.')
        if (not target.name.startswith(code_start)
                or target.suffix.lower() not in core.IMAGE_EXTENSIONS):
            raise ValueError('Map-Dateiname gehört nicht zur Asset-Kennung.')
        return target

    active_paths = set()
    for field in ('materials', 'relink_materials'):
        materials = data.get(field, {})
        if not isinstance(materials, dict):
            raise ValueError('Ungültige Materialliste.')
        for maps in materials.values():
            if not isinstance(maps, dict):
                raise ValueError('Ungültige Mapliste.')
            for item in maps.values():
                if not isinstance(item, dict):
                    raise ValueError('Ungültiger Map-Eintrag.')
                target = checked_map_path(item.get('relative_path'), item.get('path'))
                if item.get('filename') and target.name != item['filename']:
                    raise ValueError('Map-Dateiname und Pfad passen nicht zusammen.')
                managed.add(target)
                active_paths.add(target)

    owned_files = data.get('owned_files', [])
    if not isinstance(owned_files, list) or any(not isinstance(p, str) for p in owned_files):
        raise ValueError('Ungültige Liste verwalteter Maps.')
    for relative in owned_files:
        managed.add(checked_map_path(relative=relative))

    if data.get('version') == '2.02':
        # 2.02 recorded only the material's largest downscale variant. Recognize
        # its exact siblings from the saved size selection; never scan arbitrary
        # neighboring filenames and assume ownership based on a directory alone.
        sizes = data.get('texture_downscale_sizes', [])
        if not isinstance(sizes, list):
            raise ValueError('Ungültige Map-Größenliste.')
        sizes = [size for size in sizes if type(size) is int and size > 0]
        for target in active_paths:
            match = re.search(r'_[0-9]+px_([^/\\]+)$', target.name)
            if match:
                for size in sizes:
                    sibling = target.with_name(target.name[:match.start()] + '_' + str(size)
                                               + 'px_' + match.group(1))
                    if sibling.is_file():
                        managed.add(checked_map_path(absolute=sibling))
    candidate = {'manifest_path': manifest_path, 'manifest': data,
                 'metadata_storage': 'sidecar',
                 'asset_root': root, 'texture_folder': folder, 'max_file': max_file,
                 'prefix': prefix, 'managed_paths': tuple(sorted(managed, key=_path_key))}
    candidate['revision'] = _fingerprint(candidate['managed_paths'])
    # Do not pair identity from an earlier manifest with a newer file revision.
    if read_data() != data:
        raise AssetChangedError('Asset wurde während der Suche geändert. Bitte erneut auswählen.')
    return candidate


def filename_asset_id(asset_code):
    """Reproducible process identity; no extra field is stored in a MAX file."""
    return uuid.uuid5(uuid.NAMESPACE_URL, 'TencentToVray:' + str(asset_code).casefold()).hex


def _is_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return Path(path).is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _folder_inventory(folder):
    folder = Path(folder)
    if not folder.exists():
        return ()
    return tuple(sorted((path.name.casefold(), path.is_file(), _is_link(path))
                        for path in folder.iterdir() if path.name != '.tencent_asset.lock'))


def _filename_candidate(core, root, max_path, maps_root, match, ignored_sidecars=(), fingerprint=True):
    """Infer only the exact published namespace, never read or modify the MAX."""
    if _is_link(max_path):
        raise ValueError('Eine MAX-Verknüpfung ist kein verwaltetes Asset.')
    readable = match.group('base')
    requested = readable[3:] if readable.lower().startswith('ai_') else readable
    requested = core.validate_asset_name(requested)
    time_code, identity, prefix = match.group('time', 'identity', 'date')
    # New exports always use a real, automatically selected date and time.
    core.datetime.strptime(prefix + time_code, '%y%m%d%H%M')
    asset = requested + '_' + time_code + '_' + identity
    code = max_path.stem
    if core.asset_code(prefix, asset).casefold() != code.casefold():
        raise ValueError('MAX-Dateiname ist nicht eindeutig aufgebaut.')
    folder = maps_root / (time_code + '_' + identity)
    if _is_link(maps_root) or _is_link(folder) or not _inside(folder, root):
        raise ValueError('Map-Ordner darf keine Verknüpfung sein.')
    if folder.exists() and not folder.is_dir():
        raise ValueError('Map-Ziel ist kein Ordner.')
    suffixes = {value.casefold(): key for key, value in core.MAP_SUFFIX.items()}
    pattern = re.compile(r'^' + re.escape(code[:-len(prefix) - 1])
        + r'(?:_mat(?P<material>[0-9]{2,}))?_' + re.escape(prefix)
        + r'_(?:(?P<size>[1-9][0-9]*)px_)?(?P<kind>'
        + '|'.join(re.escape(value) for value in suffixes) + r')(?P<ext>\.[A-Za-z0-9]+)$', re.I)
    inventory = _folder_inventory(folder)
    variants, managed, seen = {}, {max_path.resolve()}, set()
    for path in folder.iterdir() if folder.is_dir() else ():
        if (path.resolve() in ignored_sidecars and path.name in ('import_manifest.json', 'import_report.txt')
                and path.is_file() and not _is_link(path)):
            continue
        if path.name == '.tencent_asset.lock' and path.is_file() and not _is_link(path):
            continue
        if not path.is_file() or _is_link(path):
            raise ValueError('Unbekannter Eintrag im Map-Ordner: ' + path.name)
        parsed = pattern.fullmatch(path.name)
        if parsed is None or parsed.group('ext').lower() not in core.IMAGE_EXTENSIONS:
            raise ValueError('Map-Datei kann nicht eindeutig zugeordnet werden: ' + path.name)
        material = int(parsed.group('material') or '0')
        if parsed.group('material') is not None and material == 0:
            raise ValueError('Ungültige Materialnummer: ' + path.name)
        size = int(parsed.group('size')) if parsed.group('size') else None
        kind = suffixes[parsed.group('kind').casefold()]
        key = (material, size, kind)
        if key in seen:
            raise ValueError('Mehrdeutige Map-Variante: ' + path.name)
        seen.add(key)
        variants.setdefault(material, {}).setdefault(kind, []).append((size, path))
        managed.add(path.resolve())
    if 0 in variants and len(variants) != 1:
        raise ValueError('Einzelmaterial und nummerierte Materialien sind vermischt.')
    materials, indices = {}, {}
    for index, maps in sorted(variants.items()):
        name = 'Material' if index == 0 else 'Material ' + str(index).zfill(2)
        indices[name] = index
        materials[name] = {}
        for kind, choices in maps.items():
            # Unscaled originals are the largest variant; otherwise use the
            # greatest explicitly named size. Every valid variant stays owned.
            size, path = max(choices, key=lambda item: float('inf') if item[0] is None else item[0])
            materials[name][kind] = {'path': str(path.resolve()), 'filename': path.name,
                'relative_path': path.resolve().relative_to(root).as_posix()}
    data = {'version': 'filename', 'requested_asset_name': requested, 'asset': asset,
            'asset_id': identity.lower() if len(identity) == 32 else filename_asset_id(code),
            'asset_short_code': identity if len(identity) == 8 else '',
            'asset_code': code, 'import_time_code': time_code,
            'imported_at': core.datetime.strptime(prefix + time_code, '%y%m%d%H%M').isoformat(),
            'max_file': str(max_path.resolve()), 'source_fbx': '',
            'materials': materials, 'relink_materials': materials,
            '_filename_material_indices': indices,
            'owned_files': sorted(path.relative_to(root).as_posix()
                                  for path in managed if path != max_path.resolve())}
    candidate = {'manifest_path': max_path.resolve(), 'manifest': data,
                 'metadata_storage': 'filename', 'asset_root': root,
                 'texture_folder': folder.resolve(), 'max_file': max_path.resolve(),
                 'prefix': prefix, 'managed_paths': tuple(sorted(managed, key=_path_key)),
                 'folder_inventory': inventory}
    candidate['revision'] = _fingerprint(candidate['managed_paths']) if fingerprint else None
    if _folder_inventory(folder) != inventory:
        raise AssetChangedError('Der Map-Ordner wurde während der Suche geändert. Bitte erneut vorprüfen.')
    return candidate


def _filename_pattern(core, base):
    return re.compile(r'^(?P<base>' + re.escape(core.prefixed_asset_name(base))
        + r')_(?P<time>[0-9]{4})_(?P<identity>[0-9a-f]{32}|[A-HJ-NP-Z2-9]{8})'
        + r'_(?P<date>[0-9]{6})\.max$', re.I)


def validate_filename_layout(core, candidate):
    """Check that a confirmed legacy asset remains recognizable after cleanup."""
    if candidate.get('metadata_storage') != 'sidecar':
        return
    try:
        root = Path(candidate['asset_root']).resolve()
        max_file = Path(candidate['max_file']).resolve()
        match = _filename_pattern(core, candidate['manifest']['requested_asset_name']).fullmatch(max_file.name)
        if match is None:
            raise ValueError('MAX-Dateiname entspricht nicht dem eindeutigen Namensschema.')
        owned = {Path(path).resolve() for path in candidate['managed_paths']}
        _filename_candidate(core, root, max_file, Path(candidate['texture_folder']).parent,
                            match, ignored_sidecars=owned, fingerprint=False)
    except (OSError, ValueError, TypeError, KeyError, AssetStoreError) as error:
        raise AssetStoreError('Dieses ältere Asset kann ohne seine Begleitdateien nicht eindeutig '
                              'erkannt werden. Bitte als Neu importieren. Ursache: ' + str(error)) from error


def discover_assets(core, options):
    """Recognize exact MAX/map names, with read-only support for old sidecars.

    Every candidate is shown and explicitly approved before replacement. A
    damaged old sidecar or an ambiguous map directory never falls back to a
    guessed ownership. Unknown matches remain visible to preflight as conflicts.
    """
    base = core.validate_asset_name(options.requested_asset_name or options.asset_name)
    root = Path(options.asset_folder).resolve()
    if not root.is_dir():
        return []
    expected_maps = core.maps_folder_for_target(root, base)
    maps_roots = [path for path in root.iterdir()
                  if path.is_dir() and path.name.casefold() == expected_maps.name.casefold()
                  and _inside(path, root)]
    if len(maps_roots) > 1:
        return []
    result, sidecar_folders, claimed_max = [], set(), set()
    for maps_root in maps_roots:
        for manifest_path in maps_root.glob('*/import_manifest.json'):
            sidecar_folders.add(_path_key(manifest_path.parent))
            try:
                candidate = _read_candidate(core, root, base, maps_root, manifest_path)
                if candidate is not None:
                    result.append(candidate)
                    claimed_max.add(_path_key(candidate['max_file']))
            except (OSError, ValueError, TypeError, KeyError, RecursionError, AssetStoreError):
                continue
    pattern = _filename_pattern(core, base)
    matches = [(path, pattern.fullmatch(path.name)) for path in root.iterdir()
               if path.is_file()]
    matches = [(path, match) for path, match in matches if match is not None]
    identities = {}
    for path, match in matches:
        identity = (match.group('time') + '_' + match.group('identity')).casefold()
        identities[identity] = identities.get(identity, 0) + 1
    for max_path, match in matches:
        if _path_key(max_path) in claimed_max:
            continue
        maps_root = maps_roots[0] if maps_roots else expected_maps
        identity = match.group('time') + '_' + match.group('identity')
        folder = maps_root / identity
        if identities[identity.casefold()] != 1 or _path_key(folder) in sidecar_folders:
            continue
        try:
            result.append(_filename_candidate(core, root, max_path, maps_root, match))
        except (OSError, ValueError, TypeError, KeyError, AssetStoreError):
            continue
    return sorted(result, key=lambda item: (str(item['manifest'].get('imported_at', '')),
                                           str(item['manifest_path'])), reverse=True)


class AssetLock:
    """Nonblocking cross-process lock; process exit automatically releases it.

    Windows keeps the file exclusively open and deletes it with the last handle.
    There is no separate unlock/unlink interval in which two owners could enter.
    The server enforces the same sharing contract on Windows SMB shares. Legacy
    byte-lock owners also prevent this exclusive open, so versions cannot overlap.
    The POSIX fallback keeps its inode on disk to avoid the unlink/flock race.
    """
    def __init__(self, path, create=True):
        self.path = Path(path)
        self.create = create
        self._stream = None
        self._handle = None
        self._close_handle = None

    def _enter_windows(self):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                               wintypes.HANDLE)
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        # No handle inheritance and no sharing; Windows owns deletion even after
        # abrupt process exit. Do not fall back to a local lock on network errors.
        handle = create_file(str(self.path), 0x80000000 | 0x40000000 | 0x00010000,
                             0, None, 4 if self.create else 3,
                             0x00000100 | 0x04000000 | 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):
                raise AssetBusyError('Dieses Asset wird bereits importiert. Bitte nach dessen Abschluss erneut starten.')
            raise ctypes.WinError(error)
        self._handle = handle
        self._close_handle = close_handle
        return self

    def __enter__(self):
        if self._stream is not None or self._handle is not None:
            raise AssetBusyError('Diese Asset-Sperre ist bereits aktiv.')
        # A lock path must never redirect delete-on-close to an existing map.
        # OPEN_REPARSE_POINT above also prevents following a link swapped in
        # between this check and the exclusive CreateFile call.
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if self.path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise AssetStoreError('Der Asset-Sperrpfad darf keine Verknüpfung sein.')
        if os.name == 'nt':
            return self._enter_windows()
        stream = self.path.open('a+b' if self.create else 'r+b')
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0 and self.create:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise AssetBusyError('Dieses Asset wird bereits importiert. Bitte nach dessen Abschluss erneut starten.') from exc
        self._stream = stream
        return self

    def __exit__(self, *unused):
        if self._handle is not None:
            import ctypes
            handle, self._handle = self._handle, None
            close_handle, self._close_handle = self._close_handle, None
            if not close_handle(handle):
                raise ctypes.WinError(ctypes.get_last_error())
            return
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.seek(0)
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class AssetTransaction:
    """Stage a complete import and explicitly publish its maps and MAX file.

    Pass the unmodified selected candidate returned by discover_assets. The
    caller must obtain final approval before publish(). New imports pass no candidate. The staging
    directory is on the same volume as the final files, permitting os.replace.
    A crash/power loss cannot make a multi-file operation atomic; its backups
    remain in the staging directory for recovery if the process exits early.
    """
    def __init__(self, asset_root, texture_folder, max_file, approved_candidate=None):
        self.asset_root = Path(asset_root).resolve()
        self.texture_folder = Path(texture_folder).resolve()
        self.max_file = Path(max_file).resolve()
        if (not _inside(self.texture_folder, self.asset_root)
                or self.texture_folder == self.asset_root
                or self.max_file.parent != self.asset_root
                or self.max_file.suffix.lower() != '.max'):
            raise AssetStoreError('Ungültiger Asset-Zielpfad.')
        self.candidate = approved_candidate
        self._managed = set()
        if approved_candidate is not None:
            for key, value in (('asset_root', self.asset_root), ('texture_folder', self.texture_folder),
                               ('max_file', self.max_file)):
                if _path_key(approved_candidate[key]) != _path_key(value):
                    raise AssetStoreError('Bestätigung und Asset-Ziel stimmen nicht überein.')
            if _path_key(approved_candidate['manifest_path']) not in {
                    _path_key(self.max_file),
                    _path_key(self.texture_folder / 'import_manifest.json')}:
                raise AssetStoreError('Bestätigung und Asset-Metadaten stimmen nicht überein.')
            self._managed = {Path(p).resolve() for p in approved_candidate['managed_paths']}
            for path in self._managed:
                self._validate_target(path)
        self.stage_dir = None
        self._lock = None
        self._published = False
        self._preserve_stage = False
        self._backups = {}
        self._installed = []
        self.cleanup_warning = ''
        self._journal_started = False

    def _write_journal(self, journal):
        # Keep the original recovery plan immutable; a broken connection during
        # a later update must never truncate the only record of the backups.
        # Progress is append-only, so preceding complete lines remain usable if
        # the server receives only part of the newest record.
        path = self.stage_dir / ('_progress.jsonl' if self._journal_started else '_recovery.json')
        with path.open('a' if self._journal_started else 'x', encoding='utf-8') as stream:
            json.dump(journal, stream, indent=None if self._journal_started else 2)
            if self._journal_started:
                stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        self._journal_started = True

    def _validate_target(self, path):
        path = Path(path).resolve()
        if path == self.max_file:
            return path
        if not _inside(path, self.texture_folder) or path == self.texture_folder:
            raise AssetStoreError('Datei liegt außerhalb des bestätigten Asset-Ziels: ' + str(path))
        if path.name == '.tencent_asset.lock':
            raise AssetStoreError('Die Asset-Sperre darf nicht überschrieben werden.')
        return path

    def _check_revision(self):
        if self.candidate is not None:
            if (self.candidate.get('metadata_storage') == 'filename'
                    and _folder_inventory(self.texture_folder) != self.candidate['folder_inventory']):
                raise AssetChangedError('Der Map-Ordner wurde seit der Rückfrage geändert. Bitte erneut vorprüfen.')
            if _fingerprint(self._managed) != self.candidate['revision']:
                raise AssetChangedError('Das Asset wurde seit der Rückfrage geändert. Bitte neu auswählen und erneut bestätigen.')

    def __enter__(self):
        if self._lock is not None:
            raise AssetStoreError('Die Asset-Transaktion läuft bereits.')
        self.asset_root.mkdir(parents=True, exist_ok=True)
        self.texture_folder.mkdir(parents=True, exist_ok=True)
        lock_path = self.texture_folder / '.tencent_asset.lock'
        if not _inside(lock_path, self.texture_folder):
            raise AssetStoreError('Ungültiger Pfad für die Asset-Sperre.')
        lock = AssetLock(lock_path)
        lock.__enter__()
        self._lock = lock
        try:
            self._check_revision()
            # Publish the stage directory only after its identity is complete.
            # Another importer can then distinguish unrelated work throughout
            # the long native import, before the recovery plan exists.
            preparing = Path(tempfile.mkdtemp(prefix='.tencent_prepare_', dir=str(self.asset_root)))
            try:
                with (preparing / '_asset_identity.txt').open('x', encoding='utf-8') as stream:
                    stream.write(self.max_file.name + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                staged = preparing.with_name(preparing.name.replace('.tencent_prepare_', '.tencent_stage_', 1))
                os.rename(str(preparing), str(staged))
                self.stage_dir = staged
            except BaseException:
                # Only our private pre-publication directory is eligible. A
                # remotely completed rename instead leaves an identified stage.
                if (preparing.parent == self.asset_root and preparing.name.startswith('.tencent_prepare_')
                        and not preparing.is_symlink()):
                    try:
                        shutil.rmtree(str(preparing))
                    except OSError:
                        pass
                raise
        except BaseException:
            self._lock.__exit__()
            self._lock = None
            raise
        return self

    def publish(self, staged_to_final, remove=()):
        """Publish a mapping of stage file -> final file, rolling back on errors.

        Existing files require ownership in the approved candidate. Newly added
        outputs are created without clobbering a file that appeared meanwhile.
        MAX is installed after maps. Confirmed legacy sidecars can then be
        removed within the same rollback scope; only owned paths qualify.
        """
        if self._lock is None or self.stage_dir is None or self._published:
            raise AssetStoreError('Die Asset-Transaktion ist nicht zum Speichern bereit.')
        entries, seen = [], set()
        for source, target in staged_to_final.items():
            source, target = Path(source).resolve(), self._validate_target(target)
            if not _inside(source, self.stage_dir) or not source.is_file():
                raise AssetStoreError('Vorbereitete Asset-Datei fehlt: ' + str(source))
            if _path_key(target) in seen:
                raise AssetStoreError('Doppelter Asset-Zielpfad: ' + str(target))
            seen.add(_path_key(target))
            if target.exists() and target not in self._managed:
                raise AssetStoreError('Datei gehört nicht zum bestätigten Asset und wird nicht überschrieben: ' + str(target))
            entries.append((source, target))
        entries.sort(key=lambda item: (item[1].name == 'import_manifest.json',
                                       item[1] == self.max_file, str(item[1])))
        removals = []
        for target in remove:
            target = self._validate_target(target)
            if _path_key(target) in seen:
                raise AssetStoreError('Doppelter Asset-Zielpfad: ' + str(target))
            if target not in self._managed:
                raise AssetStoreError('Nicht bestätigte Datei darf nicht entfernt werden: ' + str(target))
            if target == self.max_file:
                raise AssetStoreError('Die MAX-Datei darf nicht entfernt werden.')
            seen.add(_path_key(target))
            if target.exists():
                removals.append((None, target))
        self._check_revision()
        entries.extend(removals)
        backup_dir = self.stage_dir / '_backups'
        backup_dir.mkdir(exist_ok=False)
        backups = {}
        # Prepare every backup before touching any target file.
        for index, (_, target) in enumerate(entries):
            if target.exists():
                backup = backup_dir / str(index)
                shutil.copy2(str(target), str(backup))
                backups[target] = backup
        journal = {'status': 'prepared', 'targets': [
            {'path': str(target), 'backup': str(backups.get(target, '')),
             'operation': 'remove' if source is None else 'replace', 'state': 'pending'}
            for source, target in entries]}
        self._write_journal(journal)
        self._check_revision()
        self._backups = backups
        self._installed = []
        try:
            journal['status'] = 'publishing'
            for index, (source, target) in enumerate(entries):
                # Persist intent before each write. "attempting" means that a
                # server may have completed it even if no reply reached us.
                journal['targets'][index]['state'] = 'attempting'
                self._write_journal(journal)
                target.parent.mkdir(parents=True, exist_ok=True)
                if source is None:
                    target.unlink()
                elif target in backups:
                    os.replace(str(source), str(target))
                else:
                    # Windows rename and POSIX link both fail if an unexpected
                    # file appeared after the earlier existence check.
                    if os.name == 'nt':
                        os.rename(str(source), str(target))
                    else:
                        os.link(str(source), str(target))
                self._installed.append(target)
                journal['targets'][index]['state'] = 'completed'
            journal['status'] = 'published'
            self._write_journal(journal)
            self._published = True
        except BaseException as original:
            if _uncertain_io_error(original):
                # Do not mutate the share again or consume any backups: the
                # failing operation may already have committed remotely, and
                # the server-side lock might be lost with the connection.
                self._preserve_stage = True
                raise AssetRollbackError(
                    'Netzwerk-/Datenträgerfehler: Der Speicherstand ist unklar; '
                    'Dateien können bereits teilweise ersetzt sein. Keine automatische '
                    'Rücknahme. Alle Sicherungen und das Wiederherstellungsprotokoll '
                    'bleiben erhalten: ' + str(self.stage_dir)
                    + '\nVor dem nächsten Import diesen Stand prüfen. Ursache: '
                    + str(original)) from original
            try:
                self.rollback()
            except AssetRollbackError as exc:
                raise exc from original
            raise

    def rollback(self):
        """Restore published files while the transaction lock is still held."""
        if self._lock is None:
            raise AssetStoreError('Die Asset-Transaktion ist bereits abgeschlossen.')
        failed, remaining = [], []
        for target in reversed(self._installed):
            try:
                if target in self._backups:
                    os.replace(str(self._backups[target]), str(target))
                else:
                    target.unlink()
            except OSError as exc:
                remaining.append(target)
                failed.append(str(target) + ': ' + str(exc))
        self._installed = list(reversed(remaining))
        self._published = False
        if failed:
            self._preserve_stage = True
            raise AssetRollbackError('Speichern fehlgeschlagen; Wiederherstellung unvollständig. '
                                     'Sicherung bleibt erhalten: ' + str(self.stage_dir)
                                     + '\n' + '\n'.join(failed))

    def __exit__(self, exc_type=None, exc_value=None, traceback=None):
        try:
            if exc_type is not None and self._published:
                self.rollback()
            if self.stage_dir is not None and not self._preserve_stage:
                resolved = self.stage_dir.resolve()
                if (resolved.parent == self.asset_root and resolved.name.startswith('.tencent_stage_')
                        and not self.stage_dir.is_symlink()):
                    try:
                        shutil.rmtree(str(resolved))
                    except OSError as exc:
                        # Publication/rollback has already completed. A locked
                        # temporary file must not turn a successful scene update
                        # into a reported import failure after deleting backups.
                        self.cleanup_warning = 'Temporäre Dateien bleiben erhalten: ' + str(resolved) + ': ' + str(exc)
        finally:
            if self._lock is not None:
                self._lock.__exit__()
                self._lock = None
