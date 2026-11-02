"""Asset overwrite safety tests independent of a running 3ds Max process."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tencent_asset_store as store
import tencent_importer_core as core


class AssetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.options = SimpleNamespace(asset_folder=self.root, asset_name='Chair', requested_asset_name='')
        self.asset_id = 'a' * 32
        self.asset = 'Chair_1907_' + self.asset_id
        self.code = core.asset_code('260916', self.asset)
        self.folder = core.maps_folder_for_target(self.root, 'Chair') / ('1907_' + self.asset_id)
        self.folder.mkdir(parents=True)
        self.max_file = self.root / (self.code + '.max')
        self.map_file = self.folder / (self.code + '_basecolor.png')
        self.manifest_path = self.folder / 'import_manifest.json'
        self.report = self.folder / 'import_report.txt'
        self.max_file.write_bytes(b'old max')
        self.map_file.write_bytes(b'old map')
        self.report.write_text('old report', encoding='utf-8')
        self.data = {'version': '2.02', 'requested_asset_name': 'Chair', 'asset': self.asset,
                     'asset_id': self.asset_id, 'asset_code': self.code, 'import_time_code': '1907',
                     'imported_at': '2026-09-16T19:07:00+02:00', 'max_file': str(self.max_file),
                     'materials': {'Material': {'basecolor': {
                         'path': str(self.map_file), 'filename': self.map_file.name,
                         'relative_path': self.map_file.relative_to(self.root).as_posix(),
                         'sha256': 'historical recorded hash is not used for revision checks'}}}}
        self.write_manifest()

    def write_manifest(self):
        self.manifest_path.write_text(json.dumps(self.data), encoding='utf-8')

    def candidate(self):
        return store.discover_assets(core, self.options)[0]

    def transaction(self, candidate=None):
        return store.AssetTransaction(self.root, self.folder, self.max_file,
                                      approved_candidate=candidate or self.candidate())

    def staged(self, tx, include_new=False):
        result = {}
        for index, target in enumerate((self.max_file, self.map_file, self.manifest_path, self.report)):
            source = tx.stage_dir / ('new_' + str(index))
            source.write_bytes(b'new ' + target.name.encode())
            result[source] = target
        if include_new:
            source = tx.stage_dir / 'new_normal'
            source.write_bytes(b'new normal')
            result[source] = self.folder / (self.code + '_normal.png')
        return result

    def original_bytes(self):
        return {path: path.read_bytes() for path in (self.max_file, self.map_file, self.manifest_path, self.report)}

    def test_discovery_preserves_identity_and_validates_only_matching_name(self):
        candidate = self.candidate()
        self.assertEqual(candidate['prefix'], '260916')
        self.assertEqual(candidate['manifest']['asset_id'], self.asset_id)
        self.assertEqual(candidate['max_file'], self.max_file)
        self.assertEqual(set(candidate['managed_paths']), set(self.original_bytes()))
        self.options.asset_name = 'chair'
        self.assertEqual(len(store.discover_assets(core, self.options)), 1)
        self.options.asset_name = 'Other'
        self.assertEqual(store.discover_assets(core, self.options), [])

    def test_discovery_supports_moved_asset_folder(self):
        moved = self.root / 'moved'
        moved.mkdir()
        shutil.copytree(self.folder.parent, moved / self.folder.parent.name)
        shutil.copy2(self.max_file, moved / self.max_file.name)
        self.options.asset_folder = moved
        candidate = self.candidate()
        self.assertEqual(candidate['max_file'], moved / self.max_file.name)
        self.assertTrue(all(store._inside(path, moved) for path in candidate['managed_paths']))

    def test_discovery_rejects_map_escape_and_foreign_max_basename(self):
        original = self.data['materials']['Material']['basecolor']['relative_path']
        self.data['materials']['Material']['basecolor']['relative_path'] = '../foreign.png'
        self.write_manifest()
        self.assertEqual(store.discover_assets(core, self.options), [])
        self.data['materials']['Material']['basecolor']['relative_path'] = original
        self.data['max_file'] = str(self.root / 'foreign.max')
        self.write_manifest()
        self.assertEqual(store.discover_assets(core, self.options), [])

    def test_discovery_rejects_mismatched_identity_and_partial_manifests(self):
        self.data['asset_id'] = 'b' * 32
        self.write_manifest()
        self.assertEqual(store.discover_assets(core, self.options), [])
        self.manifest_path.write_text('{', encoding='utf-8')
        self.assertEqual(store.discover_assets(core, self.options), [])

    def test_owned_files_preserve_inactive_maps_and_variants(self):
        variant = self.folder / (self.code + '_512px_basecolor.png')
        variant.write_bytes(b'old variant')
        self.data.update(version='2.03', materials={}, owned_files=[
            self.map_file.relative_to(self.root).as_posix(), variant.relative_to(self.root).as_posix()])
        self.write_manifest()
        candidate = self.candidate()
        self.assertIn(self.map_file, candidate['managed_paths'])
        self.assertIn(variant, candidate['managed_paths'])
        with self.transaction(candidate) as tx:
            source = tx.stage_dir / 'next_map.png'
            source.write_bytes(b'restored textures')
            tx.publish({source: self.map_file})
        self.assertEqual(self.map_file.read_bytes(), b'restored textures')

    def test_relink_materials_keep_inactive_map_identity_validated(self):
        self.data['relink_materials'] = self.data.pop('materials')
        self.data['materials'] = {}
        self.write_manifest()
        self.assertIn(self.map_file, self.candidate()['managed_paths'])
        self.data['relink_materials']['Material']['basecolor']['relative_path'] = '../foreign.png'
        self.write_manifest()
        self.assertEqual(store.discover_assets(core, self.options), [])

    def test_owned_files_reject_foreign_namespace_and_non_images(self):
        for filename in ('unrelated.png', self.code + '_not_a_map.txt'):
            with self.subTest(filename=filename):
                path = self.folder / filename
                path.write_bytes(b'foreign')
                self.data['owned_files'] = [path.relative_to(self.root).as_posix()]
                self.write_manifest()
                self.assertEqual(store.discover_assets(core, self.options), [])

    def test_202_migration_recognizes_only_declared_exact_downscale_siblings(self):
        active = self.folder / (self.code + '_1024px_basecolor.png')
        self.map_file.rename(active)
        entry = self.data['materials']['Material']['basecolor']
        entry.update(path=str(active), filename=active.name,
                     relative_path=active.relative_to(self.root).as_posix())
        self.data['texture_downscale_sizes'] = [512, 1024]
        declared = self.folder / (self.code + '_512px_basecolor.png')
        undeclared = self.folder / (self.code + '_256px_basecolor.png')
        other_slot = self.folder / (self.code + '_512px_roughness.png')
        for path in (declared, undeclared, other_slot):
            path.write_bytes(b'variant')
        self.write_manifest()
        candidate = self.candidate()
        self.assertIn(declared, candidate['managed_paths'])
        self.assertNotIn(undeclared, candidate['managed_paths'])
        self.assertNotIn(other_slot, candidate['managed_paths'])
        declared.write_bytes(b'changed after approval')
        with self.assertRaises(store.AssetChangedError):
            with self.transaction(candidate):
                pass

    def test_actual_file_change_invalidates_approval_even_with_same_size_and_timestamp(self):
        for path in (self.map_file, self.max_file):
            with self.subTest(path=path):
                candidate = self.candidate()
                stat = path.stat()
                path.write_bytes(b'NEW ' + path.read_bytes()[4:])
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                with self.assertRaises(store.AssetChangedError):
                    with self.transaction(candidate):
                        self.fail('A changed asset must not acquire an approved transaction')

    def test_changed_manifest_after_confirmation_is_rejected(self):
        candidate = self.candidate()
        self.data['optimize_enabled'] = False
        self.write_manifest()
        with self.assertRaises(store.AssetChangedError):
            with self.transaction(candidate):
                pass

    def test_change_during_staging_is_rejected_before_publish(self):
        original = self.max_file.read_bytes()
        with self.transaction() as tx:
            staged = self.staged(tx)
            self.map_file.write_bytes(b'external edit')
            with self.assertRaises(store.AssetChangedError):
                tx.publish(staged)
        self.assertEqual(self.map_file.read_bytes(), b'external edit')
        self.assertEqual(self.max_file.read_bytes(), original)

    def test_success_replaces_owned_files_preserves_unreferenced_maps(self):
        orphan = self.folder / 'old_scene_map.png'
        orphan.write_bytes(b'must survive')
        with self.transaction() as tx:
            stage_dir = tx.stage_dir
            staged = self.staged(tx, include_new=True)
            expected = {target: source.read_bytes() for source, target in staged.items()}
            tx.publish(staged)
            self.assertTrue((stage_dir / '_backups').is_dir())
        self.assertFalse(stage_dir.exists())
        for target, payload in expected.items():
            self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(orphan.read_bytes(), b'must survive')

    def test_failure_mid_publish_restores_all_original_files(self):
        originals = self.original_bytes()
        real_replace = os.replace
        calls = []
        def fail_second(source, target):
            calls.append(target)
            if len(calls) == 2:
                raise PermissionError('simulated locked map')
            return real_replace(source, target)
        with self.transaction() as tx:
            staged = self.staged(tx, include_new=True)
            with patch.object(store.os, 'replace', side_effect=fail_second):
                with self.assertRaises(PermissionError):
                    tx.publish(staged)
        for target, payload in originals.items():
            self.assertEqual(target.read_bytes(), payload)
        self.assertFalse((self.folder / (self.code + '_normal.png')).exists())

    def test_ambiguous_network_commit_preserves_every_backup_and_intent(self):
        originals = self.original_bytes()
        real_replace = os.replace
        committed = []
        def commit_then_disconnect(source, target):
            real_replace(source, target)
            committed.append(Path(target))
            error = OSError('SMB server committed rename but response was lost')
            error.winerror = 64  # ERROR_NETNAME_DELETED
            raise error
        with self.assertRaisesRegex(store.AssetRollbackError, 'Speicherstand ist unklar'):
            with self.transaction() as tx:
                staged = self.staged(tx)
                with patch.object(store.os, 'replace', side_effect=commit_then_disconnect):
                    tx.publish(staged)
        self.assertEqual(len(committed), 1)
        self.assertTrue(committed[0].read_bytes().startswith(b'new '))
        journal = json.loads((tx.stage_dir / '_progress.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(journal['status'], 'publishing')
        attempts = [item for item in journal['targets'] if item['state'] == 'attempting']
        self.assertEqual([Path(item['path']) for item in attempts], committed)
        # No automatic rollback is attempted through a broken connection, and
        # all originals remain available even for the ambiguous first write.
        for item in journal['targets']:
            self.assertEqual(Path(item['backup']).read_bytes(), originals[Path(item['path'])])
        self.assertTrue(tx.stage_dir.is_dir())

    def test_ambiguous_network_remove_preserves_deleted_legacy_sidecar_backup(self):
        originals = self.original_bytes()
        real_unlink = Path.unlink
        def remove_then_disconnect(path, *args, **kwargs):
            real_unlink(path, *args, **kwargs)
            error = OSError('SMB server deleted sidecar but response was lost')
            error.winerror = 121  # ERROR_SEM_TIMEOUT
            raise error
        with self.assertRaisesRegex(store.AssetRollbackError, 'Keine automatische Rücknahme'):
            with self.transaction() as tx:
                with patch.object(Path, 'unlink', remove_then_disconnect):
                    tx.publish({}, remove=(self.manifest_path, self.report))
        self.assertFalse(self.manifest_path.exists())
        self.assertEqual(self.report.read_bytes(), originals[self.report])
        journal = json.loads((tx.stage_dir / '_progress.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(journal['targets'][0]['operation'], 'remove')
        self.assertEqual(journal['targets'][0]['state'], 'attempting')
        self.assertEqual(Path(journal['targets'][0]['backup']).read_bytes(), originals[self.manifest_path])

    def test_exception_after_publish_rolls_back_files_and_new_outputs(self):
        originals = self.original_bytes()
        with self.assertRaisesRegex(RuntimeError, 'scene update'):
            with self.transaction() as tx:
                tx.publish(self.staged(tx, include_new=True))
                raise RuntimeError('scene update failed')
        for target, payload in originals.items():
            self.assertEqual(target.read_bytes(), payload)
        self.assertFalse((self.folder / (self.code + '_normal.png')).exists())

    def test_explicit_rollback_after_publish(self):
        originals = self.original_bytes()
        with self.transaction() as tx:
            tx.publish(self.staged(tx))
            tx.rollback()
            for target, payload in originals.items():
                self.assertEqual(target.read_bytes(), payload)

    def test_embedded_manifest_candidate_and_legacy_cleanup_are_transactional(self):
        candidate = self.candidate()
        candidate['manifest_path'] = self.max_file
        candidate['metadata_storage'] = 'max'
        with self.transaction(candidate) as tx:
            source = tx.stage_dir / 'replacement.max'
            source.write_bytes(b'new max with embedded identity')
            tx.publish({source: self.max_file}, remove=(self.manifest_path, self.report))
        self.assertEqual(self.max_file.read_bytes(), b'new max with embedded identity')
        self.assertFalse(self.manifest_path.exists())
        self.assertFalse(self.report.exists())
        self.assertTrue(self.map_file.is_file())
        if os.name == 'nt':
            self.assertEqual(set(self.root.rglob('*.*')), {self.max_file, self.map_file})

    def test_legacy_cleanup_rolls_back_on_later_scene_failure(self):
        originals = self.original_bytes()
        with self.assertRaisesRegex(RuntimeError, 'scene update'):
            with self.transaction() as tx:
                source = tx.stage_dir / 'replacement.max'
                source.write_bytes(b'new max with embedded identity')
                tx.publish({source: self.max_file}, remove=(self.manifest_path, self.report))
                raise RuntimeError('scene update failed')
        for target, payload in originals.items():
            self.assertEqual(target.read_bytes(), payload)

    def test_cleanup_never_removes_unowned_paths_or_max(self):
        foreign = self.folder / 'other.json'
        foreign.write_text('unrelated')
        with self.transaction() as tx:
            for target in (foreign, self.max_file, self.root / 'outside.txt'):
                with self.assertRaises(store.AssetStoreError):
                    tx.publish({}, remove=(target,))
        self.assertEqual(foreign.read_text(), 'unrelated')

    def test_max_is_published_after_maps_before_legacy_cleanup(self):
        actual_replace, actual_unlink = os.replace, Path.unlink
        operations = []
        def tracked_replace(source, target):
            operations.append(('replace', Path(target)))
            return actual_replace(source, target)
        def tracked_unlink(path, *args, **kwargs):
            operations.append(('remove', path))
            return actual_unlink(path, *args, **kwargs)
        with self.transaction() as tx:
            new_max, new_map = tx.stage_dir / 'new.max', tx.stage_dir / 'new.png'
            new_max.write_bytes(b'new max')
            new_map.write_bytes(b'new map')
            with patch.object(store.os, 'replace', side_effect=tracked_replace), \
                    patch.object(Path, 'unlink', tracked_unlink):
                tx.publish({new_max: self.max_file, new_map: self.map_file},
                           remove=(self.manifest_path, self.report))
        self.assertEqual(operations[:4], [('replace', self.map_file), ('replace', self.max_file),
                                           ('remove', self.manifest_path), ('remove', self.report)])

    def test_rollback_failure_retains_backups_and_recovery_journal(self):
        real_replace = os.replace
        calls = []
        def fail_replace_and_rollback(source, target):
            calls.append(target)
            if len(calls) >= 2:
                raise PermissionError('simulated persistent lock')
            return real_replace(source, target)
        with self.assertRaises(store.AssetRollbackError) as error:
            with self.transaction() as tx:
                stage_dir = tx.stage_dir
                with patch.object(store.os, 'replace', side_effect=fail_replace_and_rollback):
                    tx.publish(self.staged(tx))
        self.assertIn(str(stage_dir), str(error.exception))
        self.assertTrue(stage_dir.is_dir())
        self.assertTrue((stage_dir / '_recovery.json').is_file())
        self.assertTrue(any(p.read_bytes() == b'old max' for p in (stage_dir / '_backups').iterdir()))

    def test_unowned_existing_file_cannot_be_overwritten(self):
        foreign = self.folder / 'unmanaged.png'
        foreign.write_bytes(b'foreign')
        with self.transaction() as tx:
            source = tx.stage_dir / 'replacement'
            source.write_bytes(b'replacement')
            with self.assertRaises(store.AssetStoreError):
                tx.publish({source: foreign})
        self.assertEqual(foreign.read_bytes(), b'foreign')

    def test_outside_sources_targets_and_lock_target_are_rejected(self):
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'outside')
        with self.transaction() as tx:
            source = tx.stage_dir / 'replacement'
            source.write_bytes(b'replacement')
            for mapping in ({outside: self.map_file}, {source: outside},
                            {source: self.folder / '.tencent_asset.lock'}):
                with self.assertRaises(store.AssetStoreError):
                    tx.publish(mapping)

    def test_new_import_never_overwrites_existing_max(self):
        with store.AssetTransaction(self.root, self.folder, self.max_file) as tx:
            source = tx.stage_dir / 'new.max'
            source.write_bytes(b'new max')
            with self.assertRaises(store.AssetStoreError):
                tx.publish({source: self.max_file})
        self.assertEqual(self.max_file.read_bytes(), b'old max')

    def test_unexpected_new_target_appearing_at_publish_is_not_clobbered(self):
        originals = self.original_bytes()
        new_map = self.folder / (self.code + '_normal.png')
        primitive = 'rename' if os.name == 'nt' else 'link'
        actual = getattr(os, primitive)
        def create_before_install(source, target):
            Path(target).write_bytes(b'external file arrived')
            return actual(source, target)
        with self.transaction() as tx:
            with patch.object(store.os, primitive, side_effect=create_before_install):
                with self.assertRaises(FileExistsError):
                    tx.publish(self.staged(tx, include_new=True))
        for target, payload in originals.items():
            self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(new_map.read_bytes(), b'external file arrived')

    def test_stage_cleanup_failure_does_not_invalidate_successful_publication(self):
        with patch.object(store.shutil, 'rmtree', side_effect=PermissionError('temporary file locked')):
            with self.transaction() as tx:
                tx.publish(self.staged(tx))
        self.assertIn('Temporäre Dateien', tx.cleanup_warning)
        self.assertTrue(self.max_file.read_bytes().startswith(b'new '))

    def test_same_asset_lock_is_nonblocking_and_different_assets_can_proceed(self):
        with self.transaction():
            with self.assertRaises(store.AssetBusyError):
                with self.transaction():
                    pass
            other_folder = self.root / 'different_maps' / 'identity'
            with store.AssetTransaction(self.root, other_folder, self.root / 'different.max') as other:
                self.assertTrue(other.stage_dir.is_dir())
        with self.transaction():
            pass

    def test_cross_process_lock_release_and_clean_windows_output(self):
        lock_file = self.folder / '.tencent_asset.lock'
        script = ('import sys; from pathlib import Path; '
                  'from tencent_asset_store import AssetLock, AssetBusyError\n'
                  'try:\n'
                  '    with AssetLock(Path(sys.argv[1])): print("acquired")\n'
                  'except AssetBusyError:\n'
                  '    print("busy")\n')
        def child():
            return subprocess.run([sys.executable, '-c', script, str(lock_file)],
                                  cwd=str(Path(__file__).resolve().parents[1]),
                                  capture_output=True, text=True, check=True, timeout=20).stdout.strip()
        with store.AssetLock(lock_file):
            self.assertEqual(child(), 'busy')
        self.assertEqual(lock_file.is_file(), os.name != 'nt')
        self.assertEqual(child(), 'acquired')
        self.assertEqual(lock_file.is_file(), os.name != 'nt')

    def test_crashed_owner_does_not_leave_a_stale_lock(self):
        lock_file = self.folder / '.tencent_asset.lock'
        script = ('import os, sys; from tencent_asset_store import AssetLock; '
                  'lock = AssetLock(sys.argv[1]); lock.__enter__(); os._exit(0)')
        subprocess.run([sys.executable, '-c', script, str(lock_file)],
                       cwd=str(Path(__file__).resolve().parents[1]), check=True, timeout=20)
        self.assertEqual(lock_file.is_file(), os.name != 'nt')
        with store.AssetLock(lock_file):
            pass

    def test_parallel_processes_never_overlap_during_repeated_lock_recreation(self):
        lock_file = self.folder / '.tencent_asset.lock'
        marker = self.root / 'owner_marker'
        counter = self.root / 'counter'
        counter.write_text('0')
        script = '''import os, sys, time
from pathlib import Path
from tencent_asset_store import AssetLock, AssetBusyError
lock_path, marker, counter = map(Path, sys.argv[1:])
completed = 0
deadline = time.monotonic() + 20
while completed < 15:
    if time.monotonic() > deadline:
        raise RuntimeError('Lock wait exceeded test deadline')
    try:
        with AssetLock(lock_path):
            # Independent exclusive sentinel detects overlap between owners.
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                count = int(counter.read_text())
                time.sleep(0.002)
                counter.write_text(str(count + 1))
            finally:
                os.close(fd)
                marker.unlink()
            completed += 1
    except AssetBusyError:
        time.sleep(0.003)
print(completed)
'''
        children = [subprocess.Popen([sys.executable, '-c', script, str(lock_file),
                                      str(marker), str(counter)],
                                     cwd=str(Path(__file__).resolve().parents[1]),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    for _ in range(4)]
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), '15')
        self.assertEqual(counter.read_text(), '60')
        self.assertFalse(marker.exists())
        self.assertEqual(lock_file.exists(), os.name != 'nt')

    def test_missing_probe_does_not_create_lock_file(self):
        lock_file = self.folder / '.tencent_asset.lock'
        with self.assertRaises(FileNotFoundError):
            with store.AssetLock(lock_file, create=False):
                pass
        self.assertFalse(lock_file.exists())

    def test_lock_path_cannot_redirect_to_an_existing_asset_file(self):
        lock_file = self.folder / '.tencent_asset.lock'
        try:
            lock_file.symlink_to(self.map_file)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('Creating symlinks requires Developer Mode or elevation')
            raise
        with self.assertRaises(store.AssetStoreError):
            with store.AssetLock(lock_file):
                pass
        self.assertEqual(self.map_file.read_bytes(), b'old map')
        self.assertTrue(lock_file.is_symlink())

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing and legacy msvcrt behavior')
    def test_windows_lock_coexists_safely_with_legacy_byte_lock(self):
        import msvcrt
        lock_file = self.folder / '.tencent_asset.lock'
        with lock_file.open('a+b') as stream:
            stream.write(b'0')
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            with self.assertRaises(store.AssetBusyError):
                with store.AssetLock(lock_file):
                    pass
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        with store.AssetLock(lock_file):
            # An old importer cannot open a second inode, read/write, or delete
            # the active lock, even though it does not know the new lock API.
            with self.assertRaises(PermissionError):
                lock_file.open('a+b')
            with self.assertRaises(PermissionError):
                lock_file.unlink()
        self.assertFalse(lock_file.exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows delete-on-close')
    def test_probe_of_idle_legacy_lock_cleans_up_when_released(self):
        lock_file = self.folder / '.tencent_asset.lock'
        lock_file.write_bytes(b'0')
        with store.AssetLock(lock_file, create=False):
            with self.assertRaises(store.AssetBusyError):
                with store.AssetLock(lock_file, create=False):
                    pass
        self.assertFalse(lock_file.exists())


if __name__ == '__main__':
    unittest.main()
