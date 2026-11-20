from contextlib import contextmanager, redirect_stdout
import copy
import io
import json
import os
import shlex
import stat
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import freefilesync_backup as backup


@contextmanager
def temporary_layout():
    """Provide isolated source, backup, and state paths for filesystem tests."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = {
            'root': root,
            'source': root / 'source',
            'backup': root / 'backup',
            'state': root / 'state',
            'home': root / 'home',
        }
        for key, path in paths.items():
            if key == 'root':
                continue
            path.mkdir()
        yield paths


@contextmanager
def configured_paths(paths, *, source=None):
    """Redirect all user- and mount-specific paths to a temporary layout."""
    source = Path(source or paths['source'])
    destination = paths['backup']
    home = paths['home']
    config_dir = home / '.config' / 'FreeFileSync'
    with patch.multiple(
        backup,
        HOME=home,
        SOURCE=source,
        DEST_MOUNT=destination,
        BASE=destination / 'Backups' / source.name,
        CONFIG=config_dir / 'Personal Documents.ffs_batch',
        WATCHER_CONFIG=config_dir / 'Personal Documents.ffs_real',
        WATCHER_COMMAND=home / '.local' / 'bin' / 'freefilesync-personal-documents',
        STATE=paths['state'],
    ):
        yield


def mount_identity(target, source, fstype, uuid=''):
    return {'target': str(target), 'source': source, 'fstype': fstype, 'uuid': uuid}


def profile(source, base, *, mount=None, target=None):
    return {
        'source': str(source),
        'base': str(base),
        'mount': mount or mount_identity(Path(source).parent, '/dev/source', 'ext4', 'source-id'),
        'target': target or mount_identity('/mnt/teldrive', 'teldrive-crypt:', 'fuse.rclone'),
    }


@contextmanager
def add_mount_boundaries(paths, *, tree=None):
    source_info = mount_identity(paths['root'], '/dev/source', 'ext4', 'source-id')
    destination_info = mount_identity(paths['backup'], 'teldrive-crypt:', 'fuse.rclone')
    with patch.object(backup, 'mount_info_target', return_value=source_info) as source_mount, \
         patch.object(backup, 'mount_info', return_value=destination_info) as destination_mount, \
         patch.object(backup, '_mount_tree', return_value=tree or []) as mount_tree:
        yield source_mount, destination_mount, mount_tree


def config():
    root = ET.fromstring('''<FreeFileSync XmlType="BATCH"><Synchronize>
      <Differences LeftOnly="right" LeftNewer="right" RightNewer="right" RightOnly="right"/>
      <DeletionPolicy>Versioning</DeletionPolicy><VersioningFolder Style="TimeStamp-Folder"/>
      </Synchronize><FolderPairs><Pair><Left/><Right/></Pair></FolderPairs>
      <Errors Ignore="false"/><PostSyncCommand/>
      <Batch><ErrorDialog>Cancel</ErrorDialog><PostSyncAction>None</PostSyncAction></Batch>
      </FreeFileSync>''')
    root.find('FolderPairs/Pair/Left').text = str(backup.SOURCE)
    root.find('FolderPairs/Pair/Right').text = str(backup.BASE / 'current')
    root.find('Synchronize/VersioningFolder').text = str(backup.BASE / 'versioning')
    return root


class ConfigTests(unittest.TestCase):
    def load(self, root):
        with patch.object(backup.ET, 'parse', return_value=ET.ElementTree(root)):
            return backup.load_config()

    def test_approved_config(self):
        self.assertEqual(self.load(config()).tag, 'FreeFileSync')

    def test_reject_unsafe_variants(self):
        changes = [
            ('FolderPairs/Pair/Left', 'text', '/wrong'),
            ('FolderPairs/Pair/Right', 'text', str(backup.SOURCE)),
            ('Synchronize/DeletionPolicy', 'text', 'Permanent'),
            ('Synchronize/VersioningFolder', 'text', str(backup.BASE / 'current')),
            ('Synchronize/VersioningFolder', 'Style', 'Replace'),
            ('Synchronize/Differences', 'LeftOnly', 'left'),
            ('Errors', 'Ignore', 'true'),
            ('Batch/ErrorDialog', 'text', 'Show'),
            ('Batch/PostSyncAction', 'text', 'Shutdown'),
            ('PostSyncCommand', 'text', 'unapproved-command'),
        ]
        for path, attr, value in changes:
            with self.subTest(path=path, attr=attr):
                root = config()
                node = root.find(path)
                if attr == 'text':
                    node.text = value
                else:
                    node.set(attr, value)
                with self.assertRaises(ValueError):
                    self.load(root)

    def test_reject_extra_pair_and_overrides(self):
        for kind in ('extra', 'override'):
            root = config()
            pair = root.find('FolderPairs/Pair')
            if kind == 'extra':
                root.find('FolderPairs').append(copy.deepcopy(pair))
            else:
                ET.SubElement(pair, 'Synchronize')
            with self.assertRaises(ValueError):
                self.load(root)


class MountTests(unittest.TestCase):
    def mounts(self, source_uuid=None, destination='teldrive-crypt:'):
        return [{'uuid': source_uuid or backup.SOURCE_UUID, 'fstype': 'btrfs'},
                {'source': destination, 'fstype': 'fuse.rclone'}]

    def validate(self, mounts, nested=None, source_exists=True):
        result = unittest.mock.Mock(stdout=json.dumps({'filesystems': nested or []}))
        with patch.object(backup, 'mount_info', side_effect=mounts), \
             patch.object(Path, 'resolve', lambda self: self), \
             patch.object(Path, 'is_dir', return_value=source_exists), \
             patch.object(backup.subprocess, 'run', return_value=result):
            backup.validate_mounts()

    def test_expected_mounts(self):
        self.validate(self.mounts())

    def test_wrong_source_disk(self):
        with self.assertRaises(ValueError):
            self.validate(self.mounts(source_uuid='wrong-disk'))

    def test_wrong_destination(self):
        with self.assertRaises(ValueError):
            self.validate(self.mounts(destination='other:'))

    def test_missing_source(self):
        with self.assertRaises(ValueError):
            self.validate(self.mounts(), source_exists=False)

    def test_nested_mount_refused(self):
        with self.assertRaises(ValueError):
            self.validate(self.mounts(), [{'target': '/', 'children': [
                {'target': str(backup.SOURCE / 'remote')}]}])

    def test_unmounted_destination(self):
        with self.assertRaises(subprocess_error := backup.subprocess.CalledProcessError):
            with patch.object(backup.subprocess, 'run', side_effect=subprocess_error(1, 'findmnt')):
                backup.mount_info(backup.DEST_MOUNT)


class ManifestTests(unittest.TestCase):
    def test_write_round_trip_is_private_and_preserves_previous_manifest(self):
        with temporary_layout() as paths, configured_paths(paths):
            first = {'version': 1, 'profiles': [profile(paths['source'], paths['backup'] / 'Backups' / 'source')]}
            second = {'version': 1, 'profiles': []}

            backup.write_manifest(first)
            manifest_file = backup.manifest_path()
            self.assertEqual(backup.load_manifest(), first)
            self.assertEqual(stat.S_IMODE(manifest_file.stat().st_mode), 0o600)

            backup.write_manifest(second)
            self.assertEqual(backup.load_manifest(), second)
            previous = manifest_file.with_name(manifest_file.name + '.previous')
            self.assertEqual(json.loads(previous.read_text()), first)
            self.assertEqual(stat.S_IMODE(previous.stat().st_mode), 0o600)
            self.assertEqual(sorted(p.name for p in manifest_file.parent.iterdir()),
                             [manifest_file.name, previous.name])


class ManagementTests(unittest.TestCase):
    def test_add_and_remove_missing_source_keep_existing_backup_data(self):
        with temporary_layout() as paths, configured_paths(paths), add_mount_boundaries(paths):
            source, base = paths['source'], backup.BASE
            backup.write_manifest({'version': 1, 'profiles': []})

            # Relative source paths are invalid rather than silently anchored to cwd.
            with self.assertRaises(ValueError):
                backup.add_directory('relative-source', yes=True)
            self.assertEqual(backup.load_manifest()['profiles'], [])

            with patch.object(backup, 'restart_watcher', return_value='watcher refreshed'), \
                 patch.object(backup.subprocess, 'run') as run:
                self.assertEqual(backup.add_directory(source, yes=True), 'watcher refreshed')
                saved = backup.load_manifest()
                self.assertEqual([item['source'] for item in saved['profiles']], [str(source)])
                self.assertEqual(saved['profiles'][0]['base'], str(base))
                self.assertEqual(saved['profiles'][0]['mount']['uuid'], 'source-id')
                run.assert_not_called()

                # Duplicate and nested paths cannot create a second profile.
                with self.assertRaises(ValueError):
                    backup.add_directory(source, yes=True)
                nested = source / 'nested'
                nested.mkdir()
                with self.assertRaises(ValueError):
                    backup.add_directory(nested, yes=True)
                self.assertEqual(len(backup.load_manifest()['profiles']), 1)

                sentinel = base / 'versioning' / 'prior-backup' / 'important.txt'
                sentinel.parent.mkdir(parents=True)
                sentinel.write_text('retained backup')
                source.rename(paths['root'] / 'source-removed')
                self.assertEqual(backup.remove_directory(source, yes=True), 'watcher refreshed')
                self.assertEqual(backup.load_manifest()['profiles'], [])
                self.assertEqual(sentinel.read_text(), 'retained backup')

    def test_cancelled_add_and_remove_leave_manifest_and_backup_unchanged(self):
        with temporary_layout() as paths, configured_paths(paths):
            source, base = paths['source'], backup.BASE
            original = {'version': 1, 'profiles': [profile(source, base)]}
            backup.write_manifest(original)
            sentinel = base / 'current' / 'existing.txt'
            sentinel.parent.mkdir(parents=True)
            sentinel.write_text('keep me')
            manifest_file = backup.manifest_path()
            original_bytes = manifest_file.read_bytes()

            with patch('builtins.input', return_value='n'), \
                 patch.object(backup, 'restart_watcher') as restart:
                self.assertEqual(backup.add_directory(paths['root'] / 'other', yes=False), 'cancelled')
                self.assertEqual(backup.remove_directory(source, yes=False), 'cancelled')
                restart.assert_not_called()

            self.assertEqual(manifest_file.read_bytes(), original_bytes)
            self.assertEqual(sentinel.read_text(), 'keep me')

    def test_watcher_schema_delay_and_runtime_batch_pair_mapping(self):
        with temporary_layout() as paths, configured_paths(paths):
            source, base = paths['source'], backup.BASE
            managed = profile(source, base)
            manifest = {'version': 1, 'profiles': [managed]}

            backup._write_watcher(manifest)
            watcher = ET.parse(backup.WATCHER_CONFIG).getroot()
            self.assertEqual(watcher.tag, 'FreeFileSync')
            self.assertEqual(watcher.attrib, {'XmlType': 'REAL', 'XmlFormat': '2'})
            self.assertEqual([item.text for item in watcher.findall('Directories/Item')], [str(source)])
            self.assertEqual(watcher.findtext('Delay'), '300')
            self.assertEqual(shlex.split(watcher.findtext('Commandline')), [str(backup.WATCHER_COMMAND)])
            self.assertEqual(stat.S_IMODE(backup.WATCHER_CONFIG.stat().st_mode), 0o600)

            with patch.object(backup, 'load_config', return_value=config()):
                batch = backup._profile_config(managed, 'run-123')
            pair = batch.find('FolderPairs/Pair')
            self.assertEqual(pair.findtext('Left'), str(source))
            self.assertEqual(pair.findtext('Right'), str(base / 'current'))
            self.assertEqual(batch.findtext('Synchronize/VersioningFolder'),
                             str(base / 'versioning' / 'run-123'))

    def test_empty_manifest_does_not_start_a_sync(self):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(backup.subprocess, 'run') as run:
            self.assertEqual(backup._sync_manifest({'version': 1, 'profiles': []}), 0)
        self.assertIn('nothing to sync', output.getvalue())
        run.assert_not_called()

    def test_init_migrates_only_the_approved_personal_documents_profile(self):
        with temporary_layout() as paths, configured_paths(paths), add_mount_boundaries(paths):
            source, base = backup.SOURCE, backup.BASE
            backup.CONFIG.parent.mkdir(parents=True)
            ET.ElementTree(config()).write(backup.CONFIG)
            validate_config_root = backup._validate_config_root
            with patch.object(backup, 'validate_mounts'), \
                 patch.object(backup, '_validate_config_root',
                              side_effect=lambda root: validate_config_root(root, source, base)), \
                 patch.object(backup, 'restart_watcher', return_value='watcher refreshed'):
                self.assertEqual(backup.init_directories(yes=True), 'watcher refreshed')
            migrated = backup.load_manifest()['profiles']
            self.assertEqual(len(migrated), 1)
            self.assertEqual(migrated[0]['source'], str(source))
            self.assertEqual(migrated[0]['base'], str(base))


class LauncherTests(unittest.TestCase):
    def test_overlap_refuses_before_mount_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backup, 'STATE', Path(tmp)), \
                 patch('sys.argv', ['launcher']), \
                 patch.object(backup.fcntl, 'flock', side_effect=BlockingIOError), \
                 patch.object(backup, 'validate_mounts') as validate:
                self.assertEqual(backup.main(), 75)
                validate.assert_not_called()

    def test_check_does_not_start_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(backup, 'STATE', Path(tmp)), \
                 patch.object(backup, 'manifest_path', return_value=Path(tmp) / 'missing-manifest.json'), \
                 patch('sys.argv', ['launcher', '--check']), \
                 patch.object(backup, 'validate_mounts'), \
                 patch.object(backup, 'load_config', return_value=config()), \
                 patch.object(backup.subprocess, 'run') as run:
                self.assertEqual(backup.main(), 0)
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
