"""Asset isolation, FBX-owned texture extraction and the tyOptimize pipeline."""
import hashlib
import re
import secrets
import shutil
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from tencent_fbx_metadata import inspect, object_name


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def split_packed(source, destination, include_ao=False):
    """glTF metallic-roughness: G=roughness, B=metallic; ORM additionally R=AO."""
    channels = {'roughness': 1, 'metalness': 2}
    if include_ao:
        channels['ao'] = 0
    paths = {}
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image:
        with Image.open(source) as image:
            rgb = image.convert('RGB')
            for name, channel in channels.items():
                target = destination / (name + '.png')
                rgb.getchannel(channel).save(target)
                paths[name] = target
    else:
        try:
            from PySide2.QtGui import QImage
        except ImportError:
            from PySide6.QtGui import QImage
        image = QImage(str(source)).convertToFormat(QImage.Format_RGBA8888)
        if image.isNull():
            raise ValueError('Gepackte Map kann nicht gelesen werden: ' + str(source))
        bits = image.constBits()
        raw = bytes(bits) if isinstance(bits, memoryview) else bits.asstring(image.sizeInBytes())
        for name, channel in channels.items():
            pixels = raw[channel::4]
            gray = QImage(pixels, image.width(), image.height(), image.width(), QImage.Format_Grayscale8).copy()
            target = destination / (name + '.png')
            if not gray.save(str(target), 'PNG'):
                raise IOError('Kanaltrennung fehlgeschlagen: ' + str(target))
            paths[name] = target
    return paths


def extract_material_maps(core, fbx_path, destination, report):
    objects, links = inspect(fbx_path)
    groups = {}
    for mat_id, mat in objects.items():
        if mat.name != 'Material':
            continue
        name = object_name(mat.props[1])
        if name in groups:
            raise ValueError('Mehrere FBX-Materialien mit identischem Namen: ' + name)
        folder = destination / str(mat_id)
        folder.mkdir(parents=True, exist_ok=True)
        maps = {}
        for link in links:
            if link[2] != mat_id or link[1] not in objects:
                continue
            texture = objects[link[1]]
            if texture.name != 'Texture':
                continue
            videos = list(dict.fromkeys(c[1] for c in links if c[2] == link[1]
                and c[1] in objects and objects[c[1]].name == 'Video'))
            if len(videos) != 1:
                raise ValueError('Textur ohne eindeutigen Video-Record: ' + object_name(texture.props[1]))
            video = objects[videos[0]]
            source_name = str(video.value('RelativeFilename', video.value('Filename', object_name(video.props[1])))).replace('\\', '/').split('/')[-1]
            label = object_name(texture.props[1])
            classified = core.classify_texture(Path(label + '.png')) or core.classify_texture(Path(source_name))
            slot = str(link[3]).lower() if len(link) > 3 else ''
            semantic = {'diffusecolor': 'basecolor', 'basecolor': 'basecolor', 'normalmap': 'normal',
                        'emissivecolor': 'emission', 'roughness': 'roughness', 'metalness': 'metalness', 'metallic': 'metalness'}
            map_type = classified[0] if classified else semantic.get(slot)
            if not map_type:
                report.warning('Unbekannte Texturbelegung: ' + source_name + ' / ' + slot)
                continue
            payload = video.value('Content', b'')
            if not payload:
                raise ValueError('Textur ist nicht in der FBX eingebettet: ' + source_name + '. Bitte mit Embed Media exportieren.')
            extension = '.png' if payload.startswith(core.PNG_SIGNATURE) else '.jpg' if payload.startswith(b'\xff\xd8') else Path(source_name).suffix.lower()
            if extension not in core.IMAGE_EXTENSIONS:
                raise ValueError('Unbekanntes Bildformat: ' + source_name)
            target = folder / (str(videos[0]) + '_' + core.safe_filename(Path(source_name).stem) + extension)
            target.write_bytes(payload)
            if map_type in maps and sha256(maps[map_type].source_path) != sha256(target):
                raise ValueError('Mehrdeutige Maps für ' + name + ': ' + map_type)
            maps[map_type] = core.MapHit(map_type, target, 1000)
            report.add(f'{name} / {map_type}: {source_name} (FBX Video {videos[0]}, Slot {slot})')
        packed = maps.pop('packed_metal_roughness', None)
        if packed:
            stem = packed.source_path.stem.lower()
            ao = bool(re.search(r'(?:^|_)(orm|arm)(?:_|$)', stem) or 'occlusionroughnessmetallic' in stem)
            for map_type, path in split_packed(packed.source_path, folder, ao).items():
                maps.setdefault(map_type, core.MapHit(map_type, path, 1000))
            report.add('Gepackte Map getrennt: G = Roughness, B = Metalness' + (', R = AO' if ao else ''))
        groups[name] = maps
    if not groups:
        groups['Material'] = {}
    if not any(groups.values()):
        report.warning('Geometrie-FBX ohne eingebettete PBR-Maps; neutrales V-Ray-Material wird erstellt.')
    return groups


def configured_optimizer(core, percent):
    rt = core.rt
    try:
        modifier = rt.tyOptimize()
        modifier.engine = 0
        modifier.proOptimizerPreserveUVs = True
        modifier.proOptimizerMaterialBoundaries = True
        modifier.proOptimizerPreserveSelection = False
        modifier.proOptimizerInvertSelection = False
        modifier.proOptimizerVertexPercent = float(percent)
        if abs(float(modifier.proOptimizerVertexPercent) - percent) > .001:
            raise RuntimeError('Vertex-Prozentwert wurde nicht übernommen.')
        return modifier
    except Exception as exc:
        raise RuntimeError('tyOptimize fehlt oder seine Eigenschaften sind inkompatibel: ' + str(exc)) from exc


def optimize_nodes(core, nodes, options, report):
    rt = core.rt
    stats = []
    for node in nodes:
        if rt.superClassOf(node) != rt.GeometryClass:
            continue
        start = time.perf_counter()
        original = list(rt.getPolygonCount(node))
        if not options.optimize_enabled:
            stats.append({'node': str(node.name), 'original': original, 'prepared': original,
                          'final': original, 'pre_seconds': 0.0, 'final_seconds': 0.0,
                          'optimized': False})
            report.add(f'{node.name}: Optimizer aus; Geometrie und Modifier-Stack bleiben unverändert.')
            continue
        rt.resetXForm(node)
        pre_time = 0.0
        if options.pre_optimize_80:
            pre = configured_optimizer(core, 80.0)
            pre.name = 'tyOptimize Vorbereitung 80%'
            rt.addModifier(node, pre)
            rt.resetXForm(node)
            rt.collapseStack(node)
            prepared = list(rt.getPolygonCount(node))
            pre_time = time.perf_counter() - start
        else:
            prepared = original
        final_start = time.perf_counter()
        final = configured_optimizer(core, options.optimize_percent)
        final.name = '10 cents Import'
        if options.optimize_percent == 100:
            # tyOptimize converts polygons to triangles even at 100%. Keep a true
            # pass-through and avoid an expensive no-op calculation in this case.
            final.enabled = False
            report.add('100 %: tyOptimize liegt deaktiviert bereit; zum Reduzieren im Stack aktivieren.')
        rt.addModifier(node, final)
        result = list(rt.getPolygonCount(node))  # Evaluate once with the requested value.
        item = {'node': str(node.name), 'original': original, 'prepared': prepared, 'final': result,
                'pre_seconds': round(pre_time, 3), 'final_seconds': round(time.perf_counter() - final_start, 3),
                'optimized': True}
        stats.append(item)
        report.add(f'{node.name}: Vertices {original[1]} → {prepared[1]} → {result[1]}; '
                   f'Vorstufe {item["pre_seconds"]} s, final {item["final_seconds"]} s')
    return stats


def reserve_asset(core, options):
    base = core.validate_asset_name(options.requested_asset_name or options.asset_name)
    root = options.asset_folder.resolve()
    root.mkdir(parents=True, exist_ok=True)
    created = core.datetime.now().astimezone()
    time_code = created.strftime('%H%M')
    # UUID remains the internal identity; a compact code travels with all files.
    # Atomic directory creation catches local collisions, including other Max
    # instances. Use 40 random bits and no easily confused I/O/0/1 characters.
    for _ in range(100):
        short_code = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))
        name = f'{base}_{time_code}_{short_code}'
        folder = core.maps_folder_for_target(root, base) / f'{time_code}_{short_code}'
        max_pattern = re.compile(r'^' + re.escape(core.prefixed_asset_name(name)) + r'_[A-Za-z0-9]+\.max$', re.I)
        def identity_exists():
            # Mesh-only assets have no map directory. Their identity still
            # belongs to the existing MAX, independent of its import date.
            return any(max_pattern.fullmatch(path.name) for path in root.iterdir())
        if identity_exists():
            continue
        # mkdir is an atomic reservation, including other Max instances.
        try:
            folder.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        # A concurrent mesh-only import may have completed and released its
        # now-empty directory between the initial check and our reservation.
        if identity_exists():
            try:
                folder.rmdir()
            except OSError:
                pass
            continue
        from tencent_asset_store import filename_asset_id
        asset_id = filename_asset_id(core.asset_code(options.prefix, name))
        return replace(options, asset_name=name, asset_folder=root, texture_folder=folder,
                       requested_asset_name=base, asset_id=asset_id, asset_short_code=short_code, import_time_code=time_code,
                       imported_at=created.isoformat(), rename_texture_files=True)
    raise RuntimeError('Kein freier Asset-Name.')


def assign_materials(core, nodes, materials, report, options=None):
    rt = core.rt
    if len(materials) == 1:
        core.assign_material(nodes, next(iter(materials.values())), report)
        return
    cache = {}
    def convert(old):
        if old is None:
            raise ValueError('Mesh ohne Material in FBX mit mehreren Material-Sätzen.')
        key = core.get_node_key(old)
        if key in cache:
            return cache[key]
        count = rt.getNumSubMtls(old)
        if count:
            new = rt.copy(old)
            for index in range(1, count + 1):
                sub = rt.getSubMtl(old, index)
                if sub is not None:
                    rt.setSubMtl(new, index, convert(sub))
            code = core.asset_code(options.prefix, options.asset_name) if options else 'Tencent'
            new.name = rt.uniqueName(f'MTL_{code}_Multi_')
        else:
            name = str(old.name)
            new = materials.get(name)
            if new is None:
                matches = [m for n, m in materials.items() if re.fullmatch(re.escape(n) + r'[_ ]?\d+', name)]
                if len(matches) != 1:
                    raise ValueError('FBX-Material nicht eindeutig zuzuordnen: ' + name)
                new = matches[0]
        cache[key] = new
        return new
    for node in nodes:
        if rt.superClassOf(node) == rt.GeometryClass:
            node.material = convert(node.material)


def verify_material(core, material, copied):
    """Read the finished graph back; incompatible V-Ray versions must not silently pass."""
    rt = core.rt
    slots = {'basecolor': 'texmap_diffuse', 'roughness': 'texmap_reflectionGlossiness',
             'metalness': 'texmap_metalness', 'normal': 'texmap_bump', 'emission': 'texmap_self_illumination'}
    bitmap_paths = []
    for kind, info in copied.items():
        if kind not in slots:
            continue
        prop = core.find_property(material, [slots[kind], 'texmap_selfIllumination'] if kind == 'emission' else [slots[kind]])
        bitmap = core.get_property(material, prop) if prop else None
        if kind == 'normal' and bitmap is not None:
            bitmap = bitmap.normal_map
        if bitmap is None:
            raise ValueError('V-Ray-Map-Verbindung fehlt: ' + kind)
        if Path(str(bitmap.HDRIMapName)).resolve() != info.target_path.resolve():
            raise ValueError('Falscher V-Ray-Map-Pfad: ' + kind)
        expected = core.expected_bitmap_color_settings(kind)
        if int(bitmap.color_space) != expected[0] or int(bitmap.rgbColorSpace) != expected[2]:
            raise ValueError('V-Ray-Farbraum nicht übernommen: ' + kind)
        bitmap_paths.append((bitmap, str(info.target_path.resolve())))
    if not bool(material.brdf_useRoughness):
        raise ValueError('V-Ray-Roughness-Modus ist nicht aktiv.')
    return bitmap_paths


def clear_imported_materials(core, nodes, report):
    """Remove every imported material reference before saveNodes writes the asset."""
    count = 0
    for node in nodes:
        if core.has_material_slot(node):
            node.material = None
            if node.material is not None:
                raise RuntimeError('Material konnte nicht entfernt werden: ' + str(node.name))
            count += 1
    report.add(f'Texturen aus: {count} Objekte ohne Material und Map-Verweise.')


def clear_legacy_management_props(core, nodes):
    """Strip our old annotations from new imports, preserving user properties."""
    keys = ('TencentSourceFBX', 'TencentAssetID', 'TencentAssetCode',
            'TencentOptimizerEnabled', 'TencentTexturesEnabled',
            'TencentPreOptimize80', 'TencentVertexPercent')
    for node in nodes:
        for key in keys:
            if core.rt.doesUserPropExist(node, key):
                core.rt.deleteUserProp(node, key)
                if core.rt.doesUserPropExist(node, key):
                    raise RuntimeError('Alte Importer-Verwaltungsdaten konnten nicht entfernt werden: ' + key)


def existing_asset_nodes(core, candidate):
    """Match persistent identity, never the user's short name alone."""
    identity = candidate['manifest']['asset_id']
    code = candidate['manifest']['asset_code']
    result = []
    for node in core.current_scene_objects():
        try:
            tagged = str(core.rt.getUserProp(node, 'TencentAssetID')) == identity
        except Exception:
            tagged = False
        name = str(node.name)
        if tagged or name.startswith(code + '_GEO_') or name.startswith('GRP_' + code):
            result.append(node)
    keys = {core.get_node_key(n) for n in result}
    for node in result:
        try:
            is_group = core.rt.isGroupHead(node)
        except Exception:
            is_group = False
        if is_group and any(core.get_node_key(child) not in keys for child in node.children):
            raise ValueError('Die Asset-Gruppe enthält zusätzliche fremde Objekte. Bitte diese vor dem Ersetzen aus der Gruppe lösen.')
    return result


def delete_replaced_nodes(core, nodes):
    if not nodes:
        return
    failure = None
    # pymxs automatically undoes AND swallows exceptions inside undo contexts.
    # Re-raise after the context so the file transaction also rolls back.
    with core.pymxs.undo(True, 'Tencent Asset ersetzen'):
        try:
            core.rt.delete(nodes)
            if any(core.rt.isValidNode(node) for node in nodes):
                raise RuntimeError('Bisherige Asset-Objekte konnten nicht vollständig ersetzt werden.')
        except Exception as exc:
            failure = exc
            raise
    if failure is not None:
        raise RuntimeError('Die bisherigen Asset-Objekte wurden wiederhergestellt: ' + str(failure)) from failure


def replacement_options(options, candidate):
    data = candidate['manifest']
    return replace(options, asset_name=data['asset'], requested_asset_name=data['requested_asset_name'],
                   asset_id=data['asset_id'], import_time_code=data['import_time_code'],
                   asset_short_code=data.get('asset_short_code', ''),
                   imported_at=data['imported_at'], prefix=candidate['prefix'],
                   asset_folder=candidate['asset_root'], texture_folder=candidate['texture_folder'],
                   rename_texture_files=True, save_max_file=True)


def _convert_map_extension(core, path, suffix):
    """Keep an existing relink filename even if a replacement FBX changes format."""
    target = path.with_name('converted_' + path.stem + suffix)
    try:
        from PIL import Image
    except ImportError:
        Image = None
    if Image is not None:
        with Image.open(path) as image:
            if suffix.lower() in ('.jpg', '.jpeg'):
                image = image.convert('RGB')
            image.save(target)
    else:
        image = core.QtGui.QImage(str(path))
        if image.isNull() or not image.save(str(target)):
            raise RuntimeError('Map-Format konnte für den bisherigen Dateinamen nicht übernommen werden: ' + str(path))
    path.unlink()
    return target


def staged_maps(core, copied, options, candidate, name, index, count, publications):
    previous = (candidate['manifest'].get('relink_materials') or candidate['manifest'].get('materials', {})) if candidate else {}
    indices = candidate['manifest'].get('_filename_material_indices') if candidate else None
    if indices is not None:
        wanted = 0 if count == 1 else index
        matching = [key for key, number in indices.items() if number == wanted]
        old_maps = previous.get(matching[0], {}) if len(matching) == 1 else {}
        if (0 in indices.values()) != (count == 1) and previous:
            raise ValueError('Die Materialstruktur unterscheidet sich vom vorhandenen Asset. Bitte als neue Version importieren.')
        if any(number > count for number in indices.values()):
            raise ValueError('Vorhandene Material-Maps sind nicht eindeutig zuzuordnen. Bitte als neue Version importieren.')
    else:
        old_maps = previous.get(name, {})
        if not old_maps and len(previous) == count:
            old_maps = list(previous.values())[index - 1]
    final, records = {}, {}
    for kind, item in copied.items():
        staged = item.target_path
        previous_map = old_maps.get(kind)
        target = options.texture_folder / staged.name
        if previous_map:
            target = options.asset_folder / previous_map['relative_path'] if previous_map.get('relative_path') else Path(previous_map['path'])
            target = target.resolve()
            if staged.suffix.lower() != target.suffix.lower():
                staged = _convert_map_extension(core, staged, target.suffix)
        publications[staged] = target
        final[kind] = core.CopiedMap(kind, item.source_path, target)
        records[kind] = {'path': str(target.resolve()), 'filename': target.name,
                         'relative_path': target.resolve().relative_to(options.asset_folder).as_posix(),
                         'sha256': sha256(staged)}
    return final, records


def run_import(core, options, overwrite_resolver=None, overwrite_confirm=None, _batch_token=None,
               preflight=None):
    import tencent_import_state as state
    with state.import_session(_batch_token):
        return _run_import(core, options, overwrite_resolver, overwrite_confirm, preflight)


def _run_import(core, options, overwrite_resolver, overwrite_confirm, preflight=None):
    from tencent_asset_store import AssetTransaction, discover_assets
    from tencent_preflight import PreflightRequired
    core.validate_options(options)
    if preflight is not None:
        preflight.assert_row_current(options)
    core.require_3dsmax()
    candidate = None
    if overwrite_resolver is not None:
        candidates = discover_assets(core, options)
        if candidates or preflight is not None:
            selected = overwrite_resolver(options, candidates)
            if selected is not None:
                candidate = next((item for item in candidates if item is selected), None)
                if candidate is None:
                    raise ValueError('Das gewählte Asset gehört nicht zur geprüften Auswahlliste.')
                if overwrite_confirm is None:
                    raise ValueError('Ersetzen benötigt eine ausdrückliche Bestätigung für Maps, MAX und Szenenobjekte.')
    if options.optimize_enabled and not 0 < options.optimize_percent <= 100:
        raise ValueError('tyOptimize muss größer als 0 und höchstens 100 % sein.')
    if options.import_textures and (not options.replace_imported_material or not options.create_vray_bitmap_nodes):
        raise ValueError('Der sichere Batch-Import benötigt VRayBitmap und Materialersetzung.')
    if options.optimize_enabled:
        configured_optimizer(core, options.optimize_percent)  # Fail before importing if unavailable.
    if options.import_textures:
        core.rt.VRayMtl()
        core.rt.VRayBitmap()
    report = core.Report()
    options = replace(options, fbx_file=options.fbx_file.resolve())
    before = {core.get_node_key(n) for n in core.current_scene_objects()}
    old_selection = list(core.rt.selection)
    old_nodes = existing_asset_nodes(core, candidate) if candidate else []
    new_layer = None
    if options.create_layer:
        desired_layer = core.resolve_layer_name(options)
        if desired_layer.casefold() not in {name.casefold() for name in core.scene_layer_names()}:
            new_layer = desired_layer
    allocated = None
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix='tencent_fbx_', ignore_cleanup_errors=True) as temp:
            temp = Path(temp)
            # Geometry, maps and manifest must describe one identical source,
            # even if the original file is replaced during a long import.
            temp_fbx = temp / options.fbx_file.name
            shutil.copyfile(options.fbx_file, temp_fbx)
            if preflight is not None:
                preflight.assert_source_copy(options, temp_fbx)
            groups = extract_material_maps(core, temp_fbx, temp / 'textures', report) if options.import_textures else {}
            options = replacement_options(options, candidate) if candidate else reserve_asset(core, options)
            if not candidate:
                allocated = options.texture_folder
            final_max = core.max_file_path_for_target(options.asset_folder, options.prefix, options.asset_name)
            with AssetTransaction(options.asset_folder, options.texture_folder, final_max, candidate) as transaction:
                report.section('Asset ' + core.asset_code(options.prefix, options.asset_name))
                report.add('Quelle: ' + str(options.fbx_file))
                report.add('Name: ' + options.requested_asset_name)
                report.add('Kennung: ' + (options.asset_short_code or options.asset_id))
                report.add('Importzeit: ' + options.imported_at)
                report.add('Optimizer: ' + ('An' if options.optimize_enabled else 'Aus'))
                report.add('Texturen: ' + ('An' if options.import_textures else 'Aus'))
                if options.optimize_enabled:
                    report.add('80-%-Vorstufe: ' + ('Ja' if options.pre_optimize_80 else 'Nein'))
                    report.add(f'Editierbarer tyOptimize: {options.optimize_percent:g} %')
                materials, mapping, publications, bitmap_paths = {}, {}, {}, []
                map_stage = transaction.stage_dir / 'maps'
                map_stage.mkdir()
                for index, (name, maps) in enumerate(groups.items(), 1):
                    mat_options = options if len(groups) == 1 else replace(options, asset_name=options.asset_name + f'_mat{index:02d}')
                    copied = core.copy_maps(maps, map_stage, mat_options.prefix, mat_options.asset_name,
                                            True, options.texture_downscale_sizes, report)
                    final_maps, mapping[name] = staged_maps(core, copied, options, candidate, name, index, len(groups), publications)
                    materials[name] = core.build_vray_material(final_maps, mat_options, report)
                    bitmap_paths.extend(verify_material(core, materials[name], final_maps) or [])
                for path in map_stage.iterdir():
                    if path.is_file() and path not in publications:
                        target = options.texture_folder / path.name
                        if target not in publications.values():
                            publications[path] = target
                import_start = time.perf_counter()
                nodes = core.import_fbx_and_get_nodes(temp_fbx, report)
                import_seconds = time.perf_counter() - import_start
                if options.import_textures:
                    assign_materials(core, nodes, materials, report, options)
                else:
                    clear_imported_materials(core, nodes, report)
                core.rename_nodes(nodes, options, report)
                report.section('Reset XForm / tyOptimize' if options.optimize_enabled else 'Geometrie unverändert')
                with core.pymxs.redraw(False):
                    stats = optimize_nodes(core, nodes, options, report)
                if options.group_imported_objects:
                    group = core.rt.group(nodes, name=core.rt.uniqueName('GRP_' + core.asset_code(options.prefix, options.asset_name)))
                    if group is not None:
                        nodes = list(nodes) + [group]
                clear_legacy_management_props(core, nodes)
                core.create_layer_for_nodes(nodes, options, report)
                max_stage = transaction.stage_dir / 'max'
                max_stage.mkdir()
                staged_max = core.save_max_file_to_target_folder(replace(options, asset_folder=max_stage), report, nodes)
                saved = final_max if staged_max else None
                if staged_max:
                    publications[staged_max] = final_max
                owned_maps = {path for path in publications.values() if path.parent == options.texture_folder}
                if candidate:
                    owned_maps.update(path for path in candidate['managed_paths']
                                      if path.parent == options.texture_folder and path.suffix.lower() in core.IMAGE_EXTENSIONS)
                # No additional management data is persisted. Strip only a
                # known legacy importer property
                # that saveNodes may have inherited from the current scene.
                from tencent_max_metadata import remove_manifest
                if staged_max is None:
                    raise RuntimeError('Die MAX-Datei fehlt.')
                remove_manifest(staged_max)
                legacy_sidecars = ()
                if candidate and candidate.get('metadata_storage', 'sidecar') == 'sidecar':
                    legacy_sidecars = tuple(path for path in candidate['managed_paths']
                        if path.parent == options.texture_folder
                        and path.name in ('import_manifest.json', 'import_report.txt'))
                report.section('Fertig')
                report.add(f'MAX: {saved}')
                report.add(f'Maps: {options.texture_folder}' if options.import_textures else 'Ohne Material und Maps gespeichert.')
                report.add('Nur MAX und Maps ausgegeben. Keine zusätzlichen Verwaltungsdaten gespeichert.')
                report.add(f'Gesamtdauer: {time.perf_counter() - started:.3f} s; FBX-Import: {import_seconds:.3f} s')
                if candidate:
                    report.add(f'Vorhandenes Asset ersetzt; {len(old_nodes)} bisherige Szenenobjekte entfernt. Kennung und Map-Pfade bleiben erhalten.')
                if candidate:
                    plan = {'files': [str(p) for p in publications.values()] + [str(p) for p in legacy_sidecars],
                            'scene_nodes': [str(n.name) for n in old_nodes],
                            'scene_node_keys': [str(core.get_node_key(n)) for n in old_nodes],
                            'new_node_keys': [str(core.get_node_key(n)) for n in nodes],
                            'max_file': str(final_max), 'texture_folder': str(options.texture_folder)}
                    if not overwrite_confirm(options, candidate, plan):
                        raise core.ImportCancelled('Ersetzen abgebrochen. Vorhandene Dateien und Objekte bleiben erhalten.')
                core.rt.select(nodes)
                transaction.publish(publications, remove=legacy_sidecars)
                # Reopen newly published paths after staging used final filenames.
                for bitmap, path in bitmap_paths:
                    bitmap.HDRIMapName = ''
                    bitmap.HDRIMapName = path
                delete_replaced_nodes(core, old_nodes)
            if transaction.cleanup_warning:
                report.warning(transaction.cleanup_warning)
            if not owned_maps:
                # Mesh-only assets need no empty map directory. rmdir cannot
                # remove files or another import's nonempty reservation.
                for empty_folder in (options.texture_folder, options.texture_folder.parent):
                    try:
                        empty_folder.rmdir()
                    except OSError:
                        break
        if options.create_layer:
            try:
                from tencent_layer_history import remember_layer
                remember_layer(core.resolve_layer_name(options))
            except Exception as exc:
                report.warning('Layer-Verlauf konnte nicht gespeichert werden: ' + str(exc))
        return report.text()
    except Exception as exc:
        # Roll back only nodes created by this row, including partial FBX imports.
        new = [n for n in core.current_scene_objects() if core.get_node_key(n) not in before]
        if new:
            core.rt.delete(new)
        if new_layer:
            try:
                # Max refuses to delete nonempty or active layers. Only the
                # newly created target is eligible, never pre-existing layers.
                core.rt.LayerManager.deleteLayerByName(new_layer)
            except Exception as cleanup_exc:
                report.warning('Leerer Import-Layer konnte nicht entfernt werden: ' + str(cleanup_exc))
        core.rt.select([n for n in old_selection if core.rt.isValidNode(n)])
        report.error(str(exc))
        if allocated:
            # Remove our unused reservation only if it is empty. Recovery
            # outputs or foreign files must never be deleted during cleanup.
            try:
                allocated.rmdir()
            except OSError:
                pass
        if isinstance(exc, (core.ImportCancelled, PreflightRequired)):
            raise
        raise RuntimeError(report.text()) from exc


def scan(core, options):
    core.validate_options(options)
    report = core.Report()
    if not options.import_textures:
        report.add('Texturen sind für dieses Asset ausgeschaltet; die Map-Prüfung entfällt. Gespeichert wird ohne Material und Maps.')
        return report.text()
    with tempfile.TemporaryDirectory(prefix='tencent_scan_') as temp:
        groups = extract_material_maps(core, options.fbx_file, Path(temp), report)
        for name, maps in groups.items():
            report.section('Material: ' + name)
            core.report_color_space_plan(maps, report)
    return report.text()
