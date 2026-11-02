"""Explicit, read-only import preparation and scoped overwrite approval.

The only writes during preparation are disposable files in a temporary folder
and a removed write probe beside the destination. No FBX is imported, no asset
identity is reserved, and no destination directory is created.
"""
from dataclasses import fields, is_dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import re
import tempfile

import tencent_asset_store as store
import tencent_batch_support as support


class PreflightRequired(RuntimeError):
    """The prepared queue has changed and must be checked again."""


def _path_key(path):
    # Lexical normalization keeps ready() safe on every edit; it must not touch
    # a disconnected network share or resolve symlinks in the UI event handler.
    return os.path.normcase(os.path.abspath(os.fspath(path))).casefold()


def _freeze(value):
    if isinstance(value, Path):
        return _path_key(value)
    if isinstance(value, dict):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _source_stamp(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError('FBX-Datei existiert nicht oder ist keine Datei: ' + str(path))
    stat = path.stat()
    return (_path_key(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _recovery_issue(folder, max_pattern):
    """Read only the bounded recovery plan; never follow its paths or clean it.

    Even a fully written progress record cannot certify that the subsequent
    scene change and cleanup completed. A remaining matching stage therefore
    needs review before any further import of that readable asset name.
    """
    try:
        journal = folder / '_recovery.json'
        identity = folder / '_asset_identity.txt'
        for path in (folder, journal if journal.exists() else identity):
            info = path.lstat()
            if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Verknüpfter Wiederherstellungspfad')
        if not journal.exists():
            # Native import/optimization can run for minutes before publication
            # creates its recovery plan. The identity marker was written before
            # this stage became visible, so unrelated assets remain independent.
            with identity.open('rb') as stream:
                raw_identity = stream.read(1025)
            basename = raw_identity.decode('utf-8').strip()
            if len(raw_identity) > 1024 or not re.fullmatch(
                    r'ai_[A-Za-z0-9_-]+_[0-9]{4}_(?:[0-9a-f]{32}|[A-HJ-NP-Z2-9]{8})_[A-Za-z0-9]+\.max',
                    basename, re.I):
                raise ValueError('Asset-Identität des laufenden Imports ungültig')
            if max_pattern.fullmatch(basename):
                return str(folder) + ' (Import läuft oder wurde unterbrochen; vor erneutem Import prüfen)'
            return None
        with journal.open('rb') as stream:
            raw = stream.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError('Wiederherstellungsplan zu groß')
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('targets'), list) or not data['targets']:
            raise ValueError('Wiederherstellungsplan ungültig')
        if data.get('status', 'prepared') not in ('prepared', 'publishing', 'published'):
            raise ValueError('Wiederherstellungsstatus ungültig')
        max_names = []
        for item in data['targets']:
            if not isinstance(item, dict):
                raise ValueError('Wiederherstellungsziel ungültig')
            value = item.get('path')
            if not isinstance(value, str) or not value or len(value) > 32768 or '\x00' in value:
                raise ValueError('Wiederherstellungspfad ungültig')
            path = PureWindowsPath(value)
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Wiederherstellungspfad nicht absolut')
            if any(re.search(r'[<>:"|?*]', part) for part in path.parts[1:]):
                raise ValueError('Wiederherstellungspfad enthält ungültige Zeichen')
            backup = item.get('backup', '')
            if not isinstance(backup, str) or len(backup) > 32768 or '\x00' in backup:
                raise ValueError('Sicherungspfad ungültig')
            if backup and (not PureWindowsPath(backup).is_absolute() or '..' in PureWindowsPath(backup).parts):
                raise ValueError('Sicherungspfad nicht absolut')
            if item.get('operation', 'replace') not in ('replace', 'remove'):
                raise ValueError('Wiederherstellungsvorgang ungültig')
            if path.suffix.casefold() == '.max':
                max_names.append(path.name)
        if len(max_names) != 1:
            raise ValueError('Asset-Identität im Wiederherstellungsplan fehlt')
        if not re.fullmatch(r'ai_[A-Za-z0-9_-]+_[0-9]{4}_(?:[0-9a-f]{32}|[A-HJ-NP-Z2-9]{8})_[A-Za-z0-9]+\.max',
                            max_names[0], re.I):
            raise ValueError('Asset-Identität im Wiederherstellungsplan ungültig')
        # Basenames survive moving the whole output folder or mounting a share
        # under another drive letter. No historical absolute path is accessed.
        if max_pattern.fullmatch(max_names[0]):
            return str(folder) + ' (Import läuft oder Asset-Wiederherstellung ungeklärt; vor erneutem Import prüfen)'
    except (OSError, ValueError, TypeError, RecursionError):
        return str(folder) + ' (Wiederherstellung nicht sicher zuordenbar; Zielordner vor erneutem Import prüfen)'
    return None


class PreflightSession:
    def __init__(self, core):
        self.core = core
        self._source_cache = {}
        self._approved = {}
        self._queue = None
        self._offer_overwrite = True
        self.records = []

    def invalidate(self):
        """Discard approvals while retaining successful source inspections."""
        self._queue = None
        self._approved = {}

    def _signature(self, options):
        values = ({field.name: getattr(options, field.name) for field in fields(options)}
                  if is_dataclass(options) else dict(vars(options)))
        # A raw name and its accepted normalized form describe the same choice.
        name = options.requested_asset_name or options.asset_name
        try:
            name = self.core.validate_asset_name(name)
        except (ValueError, RuntimeError):
            pass
        values['asset_name'] = name
        values['requested_asset_name'] = name
        return _freeze(values)

    def _identity(self, options):
        name = self.core.validate_asset_name(options.requested_asset_name or options.asset_name)
        return (_path_key(options.fbx_file), _path_key(options.asset_folder),
                self.core.prefixed_asset_name(name).casefold())

    def ready(self, options_list, offer_overwrite=True):
        """No filesystem access, extraction, dialogs, or scene access here."""
        try:
            signatures = tuple(self._signature(item) for item in options_list)
            return bool(signatures) and self._queue == signatures and bool(offer_overwrite) == self._offer_overwrite
        except (AttributeError, TypeError, ValueError, OSError):
            return False

    def _check_plugins(self, options):
        if options.optimize_enabled and not 0 < options.optimize_percent <= 100:
            raise ValueError('Optimizer muss größer als 0 und höchstens 100 % sein.')
        if options.import_textures and (not options.replace_imported_material or not options.create_vray_bitmap_nodes):
            raise ValueError('Der Texturimport benötigt VRayBitmap und Materialersetzung.')
        if self.core.rt is None:
            raise ValueError('Die Vorprüfung muss in 3ds Max ausgeführt werden.')
        if options.optimize_enabled:
            support.configured_optimizer(self.core, options.optimize_percent)
        if options.import_textures:
            self.core.rt.VRayMtl()
            self.core.rt.VRayBitmap()

    def _scan_source(self, options):
        """Deep source check; successful results alone are cached by check()."""
        before = _source_stamp(options.fbx_file)
        objects, unused = self.core.inspect_fbx(options.fbx_file)
        # The metadata reader deliberately skips large Geometry arrays; the
        # linked Mesh model declaration is retained and can be checked cheaply.
        if not any(item.name == 'Model' and len(item.props) > 2
                   and str(item.props[2]).casefold() == 'mesh' for item in objects.values()):
            raise ValueError('Die FBX enthält kein importierbares Mesh-Modell.')
        report = self.core.Report()
        if options.import_textures:
            with tempfile.TemporaryDirectory(prefix='tencent_preflight_') as temp:
                groups = support.extract_material_maps(self.core, options.fbx_file, Path(temp), report)
                if not any(groups.values()):
                    raise ValueError('Keine eingebetteten Maps. Texturen ausschalten, wenn dieses Mesh ohne Texturen importiert werden soll.')
                # Decode every selected map now, before importing geometry.
                paths = {hit.source_path for maps in groups.values() for hit in maps.values()}
                for path in paths:
                    self._check_image(path)
                report.add(str(len(paths)) + ' eingebettete Map(s) lesbar.')
        else:
            report.add('FBX-Mesh geprüft. Texturen aus: Import ohne Material und Maps.')
        digest = support.sha256(options.fbx_file)
        if before != _source_stamp(options.fbx_file):
            raise ValueError('Die FBX wurde während der Vorprüfung geändert. Bitte erneut vorprüfen.')
        return {'text': report.text(), 'digest': digest,
                'material_count': len(groups) if options.import_textures else 0}

    def _check_image(self, path):
        try:
            from PIL import Image
        except ImportError:
            Image = None
        if Image is not None:
            with Image.open(path) as image:
                image.load()
        elif self.core.QtGui is not None:
            image = self.core.QtGui.QImage(str(path))
            if image.isNull():
                raise ValueError('Eingebettete Map ist beschädigt oder nicht lesbar: ' + path.name)
        else:
            raise ValueError('Die Bildprüfung ist nicht verfügbar. Bitte in 3ds Max vorprüfen.')

    def _probe_target(self, options):
        root = Path(options.asset_folder).resolve()
        ancestor = root
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ValueError('Der Zielpfad oder ein übergeordneter Pfad ist kein Ordner: ' + str(ancestor))
        try:
            with tempfile.NamedTemporaryFile(prefix='.tencent_check_', dir=str(ancestor)) as probe:
                probe.write(b'check')
                probe.flush()
        except OSError as exc:
            raise ValueError('Der Zielordner ist nicht beschreibbar: ' + str(root)) from exc

    def _target_state(self, options):
        """Hash only matching, validated assets, and report unowned matches."""
        root = Path(options.asset_folder).resolve()
        if root.exists() and not root.is_dir():
            raise ValueError('Der Zielpfad ist kein Ordner: ' + str(root))
        candidates = store.discover_assets(self.core, options)
        orphans = []
        name = self.core.validate_asset_name(options.requested_asset_name or options.asset_name)
        maps_name = self.core.maps_folder_for_target(root, name).name.casefold()
        known_folders = {_path_key(item['texture_folder']) for item in candidates}
        known_max = {_path_key(item['max_file']) for item in candidates}
        # Match the exact UUID/date format, plus legacy name_date.max files.
        # A different readable name such as Chair_red must remain independent.
        max_pattern = re.compile(r'^' + re.escape(self.core.prefixed_asset_name(name))
                                 + r'(?:_[0-9]{4}_(?:[0-9a-f]{32}|[A-HJ-NP-Z2-9]{8}))?_[A-Za-z0-9]+\.max$', re.I)
        if root.is_dir():
            for entry in root.iterdir():
                if entry.name.startswith('.tencent_stage_'):
                    issue = _recovery_issue(entry, max_pattern)
                    if issue:
                        orphans.append(issue)
                elif entry.name.casefold() == maps_name:
                    if not entry.is_dir():
                        orphans.append(str(entry))
                    else:
                        for child in entry.iterdir():
                            if _path_key(child) not in known_folders:
                                orphans.append(str(child))
                elif entry.suffix.casefold() == '.max' and max_pattern.fullmatch(entry.name):
                    if _path_key(entry) not in known_max:
                        orphans.append(str(entry))
        for candidate in candidates:
            owned = {_path_key(path) for path in candidate['managed_paths']}
            folder = Path(candidate['texture_folder'])
            for path in folder.iterdir() if folder.is_dir() else ():
                if path.suffix.lower() in self.core.IMAGE_EXTENSIONS and _path_key(path) not in owned:
                    orphans.append(str(path) + ' (keinem Asset zugeordnet)')
            for path in candidate['managed_paths']:
                if path == candidate['max_file'] and not candidate['manifest'].get('max_file'):
                    continue
                if not Path(path).is_file():
                    orphans.append(str(path) + ' (fehlt)')
        # Creating the formerly absent common root during a prior successful
        # row does not invalidate other rows. Matching contents still do.
        snapshot = (_path_key(root), tuple(sorted((item['manifest']['asset_id'], item['revision'])
                                                for item in candidates)), tuple(sorted(orphans)))
        return {'candidates': candidates, 'snapshot': snapshot, 'orphans': orphans}

    def _scene_scope(self, candidate):
        if candidate is None or self.core.rt is None:
            return ()
        return tuple(sorted((str(self.core.get_node_key(node)), str(node.name))
                            for node in support.existing_asset_nodes(self.core, candidate)))

    def _plan(self, candidate, scope):
        return {'files': [str(path) for path in candidate['managed_paths']],
                'scene_nodes': [name for key, name in scope],
                'scene_node_keys': [key for key, name in scope],
                'max_file': str(candidate['max_file']),
                'texture_folder': str(candidate['texture_folder'])}

    def _probe_lock(self, candidate):
        if candidate is None:
            return
        lock_path = Path(candidate['texture_folder']) / '.tencent_asset.lock'
        if lock_path.is_file():
            try:
                with store.AssetLock(lock_path, create=False):
                    pass
            except FileNotFoundError:
                # Never recreate a removed file during a read-only check. The
                # actual transaction always takes and holds its own final lock.
                pass

    def _check_material_scope(self, options, source, candidate):
        if (candidate is None or not options.import_textures
                or candidate.get('metadata_storage') != 'filename'):
            return
        indices = set(candidate['manifest'].get('_filename_material_indices', {}).values())
        if not indices:
            return
        count = source.get('material_count')
        if type(count) is not int or count < 1:
            raise ValueError('Die Materialanzahl muss vor dem Ersetzen erneut geprüft werden.')
        if (0 in indices and count > 1) or any(index > count for index in indices):
            raise ValueError('Die Materialaufteilung passt nicht zu den vorhandenen Map-Dateinamen. '
                             'Ersetzen kann die Map-Zuordnung nicht eindeutig erhalten. Bitte Neu wählen.')

    def check(self, options_list, resolver=None, confirmer=None, progress=None, offer_overwrite=True):
        # Qt progress and decision dialogs can process events. A second importer
        # must not mutate the scene or destinations while this check is open.
        import tencent_import_state as state
        with state.batch_session():
            return self._check(options_list, resolver, confirmer, progress, offer_overwrite)

    def _check(self, options_list, resolver, confirmer, progress, offer_overwrite):
        options_list = list(options_list)
        old_approved = self._approved
        self._queue = None
        self._approved = {}
        self._offer_overwrite = bool(offer_overwrite)
        names = [item.requested_asset_name or item.asset_name for item in options_list]
        name_errors = self.core.asset_name_errors(names)
        records = []
        self.records = records
        for index, options in enumerate(options_list):
            reused = False
            try:
                if index in name_errors:
                    raise ValueError(name_errors[index])
                signature = self._signature(options)
                self.core.validate_options(options)
                self._check_plugins(options)
                self._probe_target(options)
                stamp = _source_stamp(options.fbx_file)
                source_key = (stamp, bool(options.import_textures))
                source = self._source_cache.get(source_key)
                reused = source is not None
                if source is None:
                    source = self._scan_source(options)
                    self._source_cache[source_key] = source
                target = self._target_state(options)
                if target['orphans']:
                    raise ValueError('Unvollständige oder nicht zuordenbare vorhandene Dateien: '
                                     + '; '.join(target['orphans'])
                                     + '. Bitte prüfen oder einen anderen Asset-Namen wählen.')
                candidates = target['candidates']
                previous = old_approved.get(signature)
                selected, scope = None, ()
                unchanged = (previous is not None and previous['target'] == target['snapshot']
                             and previous['source_digest'] == source['digest']
                             and previous['offer_overwrite'] == self._offer_overwrite)
                if unchanged:
                    old_id = previous['candidate_id']
                    selected = next((item for item in candidates if item['manifest']['asset_id'] == old_id), None)
                    scope = tuple(self._scene_scope(selected))
                    unchanged = scope == previous['scope']
                if not unchanged and candidates and offer_overwrite:
                    if resolver is None:
                        raise ValueError('Vorhandenes Asset gefunden. Bitte Neu oder Ersetzen auswählen.')
                    selected = resolver(options, candidates)
                    if selected is not None and not any(selected is item for item in candidates):
                        raise ValueError('Das ausgewählte Asset gehört nicht zu den geprüften Treffern.')
                    scope = tuple(self._scene_scope(selected))
                elif not unchanged:
                    selected, scope = None, ()
                if selected is not None:
                    store.validate_filename_layout(self.core, selected)
                self._check_material_scope(options, source, selected)
                if (not unchanged and selected is not None
                        and (confirmer is None or not confirmer(options, selected, self._plan(selected, scope)))):
                    raise ValueError('Ersetzen wurde nicht bestätigt. Bitte erneut vorprüfen oder einen anderen Namen wählen.')
                self._probe_lock(selected)
                self._approved[signature] = {
                    'signature': signature, 'identity': self._identity(options), 'source_stamp': stamp,
                    'source_digest': source['digest'], 'target': target['snapshot'],
                    'candidate_id': selected['manifest']['asset_id'] if selected else None,
                    'candidate_revision': selected['revision'] if selected else None,
                    'scope': scope, 'offer_overwrite': self._offer_overwrite,
                }
                action = 'Ersetzen bestätigt' if selected else 'Neu bestätigt' if candidates else 'Ziel frei'
                record = {'success': True, 'status': 'Geprüft', 'text': source['text'] + '\n' + action + '.', 'reused': reused}
            except Exception as exc:
                record = {'success': False, 'status': 'Name prüfen' if index in name_errors else 'Prüfen',
                          'text': str(exc), 'reused': reused}
                if index in name_errors:
                    record['name_error'] = name_errors[index]
            records.append(record)
            if progress is not None:
                progress(index, len(options_list), record)
        if options_list and all(record['success'] for record in records):
            self._queue = tuple(self._signature(item) for item in options_list)
        return records

    def _entry(self, options, transformed=False):
        entry = self._approved.get(self._signature(options))
        if entry is None and transformed:
            identity = self._identity(options)
            entry = next((item for item in self._approved.values() if item['identity'] == identity), None)
        if self._queue is None or entry is None:
            raise PreflightRequired('Die Importliste ist nicht freigegeben. Bitte zuerst Vorprüfen.')
        return entry

    def _reject(self, message):
        self._queue = None
        raise PreflightRequired(message + ' Bitte erneut vorprüfen.')

    def _assert_entry_current(self, options, entry):
        try:
            if _source_stamp(options.fbx_file) != entry['source_stamp'] or support.sha256(options.fbx_file) != entry['source_digest']:
                source_path = _path_key(options.fbx_file)
                self._source_cache = {key: value for key, value in self._source_cache.items()
                                      if key[0][0] != source_path}
                self._reject('Die FBX wurde seit der Vorprüfung geändert.')
            self._check_plugins(options)
            self._probe_target(options)
            target = self._target_state(options)
            if target['snapshot'] != entry['target']:
                self._reject('Die vorhandenen Dateien im Zielordner haben sich geändert.')
            selected = next((item for item in target['candidates']
                             if item['manifest']['asset_id'] == entry['candidate_id']), None)
            self._probe_lock(selected)
            if tuple(self._scene_scope(selected)) != entry['scope']:
                self._reject('Die vorhandenen Asset-Objekte in der Szene haben sich geändert.')
        except PreflightRequired:
            raise
        except Exception as exc:
            self._reject(str(exc))

    def assert_current(self, options_list):
        options_list = list(options_list)
        if not self.ready(options_list, self._offer_overwrite):
            self._reject('Die Importliste wurde geändert oder noch nicht vollständig geprüft.')
        for index, options in enumerate(options_list):
            try:
                self._assert_entry_current(options, self._entry(options))
            except PreflightRequired as exc:
                exc.row_index = index
                raise

    def assert_row_current(self, options):
        self._assert_entry_current(options, self._entry(options))

    def assert_source_copy(self, options, path):
        """Authorize one immutable temporary snapshot used by this import."""
        entry = self._entry(options)
        try:
            matches = support.sha256(path) == entry['source_digest']
        except OSError as exc:
            self._reject('Die vorbereitete FBX-Kopie konnte nicht geprüft werden: ' + str(exc))
        if not matches:
            source_path = _path_key(options.fbx_file)
            self._source_cache = {key: value for key, value in self._source_cache.items()
                                  if key[0][0] != source_path}
            self._reject('Die FBX wurde beim Vorbereiten des Imports geändert.')

    def resolve(self, options, candidates):
        entry = self._entry(options)
        fresh = tuple(sorted((item['manifest']['asset_id'], item['revision']) for item in candidates))
        if fresh != entry['target'][1]:
            self._reject('Die vorhandenen Assets stimmen nicht mehr mit der Vorprüfung überein.')
        if entry['candidate_id'] is None:
            return None
        selected = next((item for item in candidates if item['manifest']['asset_id'] == entry['candidate_id']
                         and item['revision'] == entry['candidate_revision']), None)
        if selected is None:
            self._reject('Das bestätigte Asset ist nicht mehr unverändert vorhanden.')
        return selected

    def confirm(self, options, candidate, plan):
        entry = self._entry(options, transformed=True)
        if (candidate['manifest']['asset_id'], candidate['revision']) != (entry['candidate_id'], entry['candidate_revision']):
            self._reject('Die Freigabe gehört nicht zu diesem Asset-Stand.')
        plan_scope = tuple(sorted(zip((str(key) for key in plan.get('scene_node_keys', [])), plan['scene_nodes'])))
        if plan_scope != entry['scope']:
            self._reject('Die zu ersetzenden Szenenobjekte wurden geändert.')
        new_keys = {str(key) for key in plan.get('new_node_keys', [])}
        scope = tuple(item for item in self._scene_scope(candidate) if item[0] not in new_keys)
        if scope != entry['scope']:
            self._reject('Die vorhandenen Asset-Objekte in der Szene haben sich geändert.')
        return True
