#!/usr/bin/env python3
"""Guarded FreeFileSync launcher and managed-directory CLI."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

HOME = Path.home()
SOURCE = Path('/mnt/HDD1/Personal Documents')
SOURCE_MOUNT = Path('/mnt/HDD1')
SOURCE_UUID = 'ec34925a-82d1-4b2a-8fd0-d7eed0a726d6'
DEST_MOUNT = HOME / 'Mounts/teldrive-crypt'
BASE = DEST_MOUNT / 'Backups/Personal Documents'
CONFIG = HOME / '.config/FreeFileSync/Personal Documents.ffs_batch'
WATCHER_CONFIG = HOME / '.config/FreeFileSync/Personal Documents.ffs_real'
WATCHER_COMMAND = HOME / '.local/bin/freefilesync-personal-documents'
STATE = HOME / '.local/state/freefilesync-personal-documents'
MANIFEST_NAME = 'managed-directories.json'


def manifest_path():
    """Return the private manifest path without creating its parent directory."""
    return HOME / '.config/FreeFileSync' / MANIFEST_NAME


def mount_info(path):
    result = subprocess.run(
        ['findmnt', '--json', '--mountpoint', str(path), '-o', 'TARGET,SOURCE,FSTYPE,UUID'],
        capture_output=True, text=True, check=True, timeout=15)
    entries = json.loads(result.stdout)['filesystems']
    if len(entries) != 1:
        raise ValueError(f'Ambiguous mount: {path}')
    return entries[0]


def mount_info_target(path):
    """Find the filesystem actually containing path (which need not be a mountpoint)."""
    result = subprocess.run(
        ['findmnt', '--json', '--target', str(path), '-o', 'TARGET,SOURCE,FSTYPE,UUID'],
        capture_output=True, text=True, check=True, timeout=15)
    entries = json.loads(result.stdout)['filesystems']
    if len(entries) != 1:
        raise ValueError(f'Ambiguous filesystem for: {path}')
    return entries[0]


def _entries(tree):
    for entry in tree:
        yield entry
        yield from _entries(entry.get('children', []))


def _mount_tree():
    result = subprocess.run(['findmnt', '--json', '-o', 'TARGET,SOURCE,FSTYPE,UUID'],
                            capture_output=True, text=True, check=True, timeout=15)
    return list(_entries(json.loads(result.stdout)['filesystems']))


def _identity(info):
    return {key: info.get(key) or '' for key in ('target', 'uuid', 'fstype', 'source')}


def _reject_mount_overlap(source):
    source = Path(source)
    dest = DEST_MOUNT
    if source == dest or source in dest.parents or dest in source.parents:
        raise ValueError(f'Source overlaps the backup mount: {source}')


def validate_mounts():
    """Compatibility validation for the original Personal Documents job."""
    source_mount = mount_info(SOURCE_MOUNT)
    if source_mount['uuid'] != SOURCE_UUID or source_mount['fstype'] != 'btrfs':
        raise ValueError('Expected source disk is not mounted')
    dest = mount_info(DEST_MOUNT)
    if dest['source'] != 'teldrive-crypt:' or dest['fstype'] != 'fuse.rclone':
        raise ValueError('Expected TelDrive crypt mount is not mounted')
    for path in (SOURCE_MOUNT, SOURCE, DEST_MOUNT, BASE, BASE / 'current', BASE / 'versioning'):
        if path.resolve() != path:
            raise ValueError(f'Symlink or redirected path refused: {path}')
    if not SOURCE.is_dir():
        raise ValueError('Source directory is missing')
    # Refuse newly introduced nested mounts, which could substitute remote data.
    result = subprocess.run(['findmnt', '--json', '-o', 'TARGET'],
                            capture_output=True, text=True, check=True, timeout=15)

    def check(entries):
        for entry in entries:
            target = Path(entry['target'])
            if target == SOURCE or SOURCE in target.parents or BASE in target.parents:
                raise ValueError(f'Unexpected nested mount: {target}')
            check(entry.get('children', []))

    check(json.loads(result.stdout)['filesystems'])


def _validate_config_root(root, source=SOURCE, base=BASE, versioning=None):
    if root.tag != 'FreeFileSync' or root.get('XmlType') != 'BATCH':
        raise ValueError('Not a FreeFileSync batch job')
    pairs = root.findall('FolderPairs/Pair')
    if len(pairs) != 1 or pairs[0].findtext('Left') != str(source) or pairs[0].findtext('Right') != str(base / 'current'):
        raise ValueError('Unexpected source/destination pair')
    sync = root.find('Synchronize')
    expected = dict(LeftOnly='right', LeftNewer='right', RightNewer='right', RightOnly='right')
    if sync is None or sync.find('Differences') is None or sync.find('Differences').attrib != expected:
        raise ValueError('Expected one-way mirror directions')
    expected_versioning = versioning or base / 'versioning'
    if sync.findtext('DeletionPolicy') != 'Versioning' or sync.findtext('VersioningFolder') != str(expected_versioning):
        raise ValueError('Expected separate versioning directory')
    versioning = sync.find('VersioningFolder')
    if versioning is None or versioning.get('Style') != 'TimeStamp-Folder':
        raise ValueError('Expected timestamped versioning')
    if any(pair.find(x) is not None for pair in pairs for x in ('Synchronize', 'Compare', 'Filter')):
        raise ValueError('Per-pair overrides are not approved')
    errors = root.find('Errors')
    batch = root.find('Batch')
    if batch is None or root.findtext('Batch/ErrorDialog') != 'Cancel' or errors is None or errors.get('Ignore') != 'false':
        raise ValueError('Batch must cancel on errors')
    if root.findtext('PostSyncCommand') or root.findtext('Batch/PostSyncAction') != 'None':
        raise ValueError('Post-sync actions are not approved')
    return root


def load_config():
    """Load and validate the legacy batch template before specializing a run."""
    return _validate_config_root(ET.parse(CONFIG).getroot())


def load_manifest():
    """Read the source-of-truth manifest without creating it."""
    path = manifest_path()
    if path.parent.resolve() != path.parent:
        raise ValueError(f'Redirected manifest directory refused: {path.parent}')
    if path.is_symlink():
        raise ValueError(f'Symlinked manifest refused: {path}')
    if not path.exists():
        return {'version': 1, 'profiles': []}
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'Invalid directory manifest: {error}') from error
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or not isinstance(manifest.get('profiles'), list):
        raise ValueError('Unsupported directory manifest format')
    sources, bases = set(), set()
    source_paths = []
    for profile in manifest['profiles']:
        if not isinstance(profile, dict) or not isinstance(profile.get('source'), str) or not isinstance(profile.get('base'), str):
            raise ValueError('Malformed directory profile')
        if not isinstance(profile.get('mount'), dict) or not isinstance(profile.get('target'), dict):
            raise ValueError('Malformed mount identity in directory profile')
        source, base = Path(profile['source']), Path(profile['base'])
        if not source.is_absolute() or not base.is_absolute() or source in sources or base in bases:
            raise ValueError('Duplicate or non-absolute path in directory manifest')
        if source == Path('/') or '..' in source.parts or '..' in base.parts:
            raise ValueError('Unsafe path in directory manifest')
        _reject_mount_overlap(source)
        if base != DEST_MOUNT / 'Backups' / source.name:
            raise ValueError(f'Unexpected backup destination for {source}')
        sources.add(source)
        bases.add(base)
        source_paths.append(source)
    for index, source in enumerate(source_paths):
        if any(source in other.parents or other in source.parents for other in source_paths[index + 1:]):
            raise ValueError('Nested source paths in directory manifest')
    return manifest


def write_manifest(manifest):
    """Atomically replace the private JSON manifest, retaining one private prior copy."""
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or not isinstance(manifest.get('profiles'), list):
        raise ValueError('Invalid directory manifest')
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() != path.parent:
        raise ValueError(f'Redirected manifest directory refused: {path.parent}')
    previous = path.with_name(path.name + '.previous')
    if path.exists():
        if path.is_symlink():
            raise ValueError(f'Symlinked manifest refused: {path}')
        fd, backup_tmp = tempfile.mkstemp(prefix='.managed-directories.previous-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out, path.open('rb') as old:
                shutil.copyfileobj(old, out)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(backup_tmp, 0o600)
            os.replace(backup_tmp, previous)
        finally:
            if os.path.exists(backup_tmp):
                os.unlink(backup_tmp)
    fd, tmp = tempfile.mkstemp(prefix='.managed-directories-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def _run_lock():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    with (STATE / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Backup or directory management is already running')
        yield


def _no_symlinks(path):
    path = Path(path)
    if path.resolve() != path:
        raise ValueError(f'Symlink or redirected path refused: {path}')


def _validate_source_for_add(source):
    source = Path(source)
    if not source.is_absolute() or source == Path('/'):
        raise ValueError('Source must be an absolute directory other than /')
    _no_symlinks(source)
    _no_symlinks(source.parent)
    if not source.is_dir():
        raise ValueError(f'Source directory is missing: {source}')
    _reject_mount_overlap(source)
    source_info = mount_info_target(source)
    mount_target = Path(source_info['target'])
    _no_symlinks(mount_target)
    fstype = str(source_info.get('fstype') or '').lower()
    if fstype.startswith('fuse') or fstype in {'nfs', 'nfs4', 'cifs', 'smb3', 'sshfs'}:
        raise ValueError(f'Remote filesystem sources are not supported: {source_info.get("fstype")}')
    base = DEST_MOUNT / 'Backups' / source.name
    for entry in _mount_tree():
        target = Path(entry['target'])
        if target != mount_target and mount_target in target.parents and (target == source or source in target.parents):
            raise ValueError(f'Unexpected nested mount in source: {target}')
        if target != DEST_MOUNT and (target == base or base in target.parents or
                                    (target in base.parents and DEST_MOUNT in target.parents)):
            raise ValueError(f'Unexpected nested mount in backup destination: {target}')
    dest = mount_info(DEST_MOUNT)
    if dest.get('fstype') != 'fuse.rclone' or dest.get('source') != 'teldrive-crypt:':
        raise ValueError('Expected TelDrive crypt destination mount is not mounted')
    _no_symlinks(DEST_MOUNT)
    if not source.name or source.name in {'.', '..'}:
        raise ValueError('Source must have a usable directory name')
    return {
        'source': str(source),
        'base': str(base),
        'mount': _identity(source_info),
        'target': _identity(dest),
    }


def _validate_profile(profile, require_source=True):
    source, base = Path(profile['source']), Path(profile['base'])
    _no_symlinks(source)
    _no_symlinks(DEST_MOUNT)
    _no_symlinks(base)
    _no_symlinks(base / 'current')
    _no_symlinks(base / 'versioning')
    if require_source and not source.is_dir():
        raise ValueError(f'Source directory is missing: {source}')
    _reject_mount_overlap(source)
    if require_source:
        actual = _identity(mount_info_target(source))
        if actual != profile['mount']:
            raise ValueError(f'Source filesystem identity changed: {source}')
        fstype = str(actual.get('fstype') or '').lower()
        if fstype.startswith('fuse') or fstype in {'nfs', 'nfs4', 'cifs', 'smb3', 'sshfs'}:
            raise ValueError(f'Remote filesystem sources are not supported: {source}')
        for entry in _mount_tree():
            target = Path(entry['target'])
            source_mount = Path(actual['target'])
            if target != source_mount and source_mount in target.parents and (target == source or source in target.parents):
                raise ValueError(f'Unexpected nested mount in source: {target}')
            if target != DEST_MOUNT and (target == base or base in target.parents or
                                        (target in base.parents and DEST_MOUNT in target.parents)):
                raise ValueError(f'Unexpected nested mount in backup destination: {target}')
    dest = _identity(mount_info(DEST_MOUNT))
    if dest != profile['target'] or dest.get('fstype') != 'fuse.rclone' or dest.get('source') != 'teldrive-crypt:':
        raise ValueError('Expected TelDrive crypt destination mount identity changed')


def _confirm(action, source, yes):
    if yes:
        return True
    try:
        answer = input(f'{action} managed backup directory {source}? [y/N] ')
    except EOFError:
        answer = ''
    return answer.strip().lower() in {'y', 'yes'}


def _write_watcher(manifest):
    root = ET.Element('FreeFileSync', {'XmlType': 'REAL', 'XmlFormat': '2'})
    directories = ET.SubElement(root, 'Directories')
    for profile in manifest['profiles']:
        ET.SubElement(directories, 'Item').text = profile['source']
    ET.SubElement(root, 'Delay').text = '300'
    ET.SubElement(root, 'Commandline').text = shlex.quote(str(WATCHER_COMMAND))
    WATCHER_CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if WATCHER_CONFIG.parent.resolve() != WATCHER_CONFIG.parent:
        raise ValueError(f'Redirected watcher directory refused: {WATCHER_CONFIG.parent}')
    fd, tmp = tempfile.mkstemp(prefix='.realtimesync-', dir=WATCHER_CONFIG.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            ET.ElementTree(root).write(out, encoding='utf-8', xml_declaration=True)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, WATCHER_CONFIG)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _owned_watcher_pids():
    expected = [ '/usr/bin/RealTimeSync', str(WATCHER_CONFIG) ]
    pids = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
            if [part.decode(errors='replace') for part in argv] == expected:
                pids.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return pids


def _is_owned_watcher(pid):
    try:
        argv = (Path('/proc') / str(pid) / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
        return [part.decode(errors='replace') for part in argv] == [
            '/usr/bin/RealTimeSync', str(WATCHER_CONFIG)]
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False


def restart_watcher():
    """Refresh watcher XML and restart only the exact owned RealTimeSync argv.

    Does not start a watcher that was not already running. Returns a status string.
    """
    manifest = load_manifest()
    _write_watcher(manifest)
    pids = _owned_watcher_pids()
    if not pids:
        return 'watcher config updated; no owned watcher was running'
    for pid in pids:
        # Re-read argv immediately before signaling to avoid acting on a recycled PID.
        if _is_owned_watcher(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_is_owned_watcher(pid) for pid in pids):
        time.sleep(0.05)
    if any(_is_owned_watcher(pid) for pid in pids):
        raise RuntimeError('Owned RealTimeSync did not stop; watcher config updated but not restarted')
    if not manifest['profiles']:
        return 'watcher config updated; owned watcher stopped because no sources remain'
    log_path = STATE / 'watcher.log'
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with log_path.open('ab') as log:
        process = subprocess.Popen(['/usr/bin/RealTimeSync', str(WATCHER_CONFIG)], stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)
    if process.poll() is not None:
        raise RuntimeError(f'RealTimeSync exited during startup; inspect {log_path}')
    return 'watcher config updated; owned watcher restarted'


def _commit_profiles(manifest):
    write_manifest(manifest)
    try:
        return restart_watcher()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ET.ParseError) as error:
        return f'WARNING: manifest updated but watcher refresh failed: {error}'


def _print_management_result(message):
    print(message)
    return 1 if message.startswith('WARNING:') else 0


def add_directory(path, yes=False):
    """Validate and add one source profile; returns the watcher status string."""
    if not Path(os.path.expanduser(str(path))).is_absolute():
        raise ValueError('Source must be an absolute directory')
    source = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not _confirm('Add', source, yes):
        return 'cancelled'
    with _run_lock():
        manifest = load_manifest()
        if not manifest_path().exists():
            raise ValueError('Initialize the existing job first with `freefilesync-backup dirs init`')
        for current in manifest['profiles']:
            existing = Path(current['source'])
            if source == existing or source in existing.parents or existing in source.parents:
                raise ValueError(f'Duplicate or nested managed source: {source}')
        profile = _validate_source_for_add(source)
        if any(Path(item['base']).name == Path(profile['base']).name for item in manifest['profiles']):
            raise ValueError(f'Destination basename collision: {Path(profile["base"]).name}')
        base = Path(profile['base'])
        _no_symlinks(base)
        if base.exists():
            raise ValueError(f'Existing backup destination will not be adopted: {base}')
        manifest['profiles'].append(profile)
        return _commit_profiles(manifest)


def remove_directory(path, yes=False):
    """Remove a profile without requiring its source to exist; never touches backup data."""
    if not Path(os.path.expanduser(str(path))).is_absolute():
        raise ValueError('Source must be an absolute directory')
    source = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not _confirm('Remove', source, yes):
        return 'cancelled'
    with _run_lock():
        manifest = load_manifest()
        updated = [profile for profile in manifest['profiles'] if Path(profile['source']) != source]
        if len(updated) == len(manifest['profiles']):
            raise ValueError(f'Not a managed source: {source}')
        manifest['profiles'] = updated
        return _commit_profiles(manifest)


def init_directories(yes=False):
    """Explicitly migrate the approved default config into a manifest; never sync."""
    if not _confirm('Initialize/migrate', SOURCE, yes):
        return 'cancelled'
    with _run_lock():
        manifest = load_manifest()
        if manifest_path().exists():
            return 'managed directory manifest already initialized'
        _validate_config_root(ET.parse(CONFIG).getroot())
        validate_mounts()
        source_info = mount_info_target(SOURCE)
        target_info = mount_info(DEST_MOUNT)
        profile = {'source': str(SOURCE), 'base': str(BASE),
                   'mount': _identity(source_info), 'target': _identity(target_info)}
        _validate_profile(profile)
        manifest['profiles'].append(profile)
        return _commit_profiles(manifest)


def _format_profiles(manifest):
    if not manifest['profiles']:
        print('No managed backup directories.')
        return
    for profile in manifest['profiles']:
        print(f"{profile['source']} -> {profile['base']}")


def _profile_config(profile, run_id):
    root = load_config()
    source, base = Path(profile['source']), Path(profile['base'])
    pairs = root.findall('FolderPairs/Pair')
    pairs[0].find('Left').text = str(source)
    pairs[0].find('Right').text = str(base / 'current')
    versioning = base / 'versioning' / run_id
    root.find('Synchronize/VersioningFolder').text = str(versioning)
    return _validate_config_root(root, source, base, versioning)


def _sync_manifest(manifest, check=False):
    if not manifest['profiles']:
        print('No managed backup directories; nothing to sync.')
        return 0
    status = 0
    valid = []
    try:
        dest = _identity(mount_info(DEST_MOUNT))
        if dest.get('fstype') != 'fuse.rclone' or dest.get('source') != 'teldrive-crypt:':
            raise ValueError('Expected TelDrive crypt destination mount is not mounted')
        _no_symlinks(DEST_MOUNT)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'Destination preflight failed: {error}', flush=True)
        return 2
    for profile in manifest['profiles']:
        try:
            _validate_profile(profile)
            _profile_config(profile, 'validation')
            valid.append(profile)
        except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError, KeyError, AttributeError) as error:
            print(f"{profile.get('source', '<unknown>')}: preflight failed: {error}", flush=True)
            status = 2
    if check:
        if status == 0:
            print(f'PASS: validated {len(valid)} managed source profile(s), mount identities, mirror and versioning configuration.')
        return status
    for profile in valid:
        source, base = Path(profile['source']), Path(profile['base'])
        run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
        try:
            _validate_profile(profile)
            root = _profile_config(profile, run_id)
            # Create destination directories only after source, mount, and template checks.
            base.mkdir(parents=True, exist_ok=True)
            (base / 'current').mkdir(exist_ok=True)
            (base / 'versioning').mkdir(exist_ok=True)
            _validate_profile(profile)
            with tempfile.TemporaryDirectory(prefix='run-', dir=STATE) as tmp:
                batch = Path(tmp) / f'{source.name}.ffs_batch'
                ET.ElementTree(root).write(batch, encoding='utf-8', xml_declaration=True)
                _validate_profile(profile)
                print(f'{source}: {run_id}: starting backup; FreeFileSync success does not prove cache upload completion.', flush=True)
                result = subprocess.run(['/usr/bin/FreeFileSync', str(batch)], check=False)
            print(f'{source}: {run_id}: FreeFileSync exit {result.returncode}', flush=True)
            status = max(status, result.returncode if result.returncode >= 0 else 2)
        except (OSError, ValueError, subprocess.SubprocessError, ET.ParseError, KeyError, AttributeError) as error:
            print(f'{source}: {run_id}: backup failed/refused: {error}', flush=True)
            status = max(status, 2)
    (STATE / 'last-result.json').write_text(json.dumps({
        'finished_utc': datetime.now(timezone.utc).isoformat(),
        'exit_code': status, 'remote_upload_verified': False,
        'profiles': [profile['source'] for profile in manifest['profiles']],
    }, indent=2) + '\n', encoding='utf-8')
    return status


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Validate only; do not sync')
    commands = parser.add_subparsers(dest='command')
    dirs = commands.add_parser('dirs', help='Manage guarded backup directories')
    actions = dirs.add_subparsers(dest='dirs_action', required=True)
    init = actions.add_parser('init', help='Explicitly migrate the approved default profile')
    init.add_argument('--yes', action='store_true', help='Confirm for automation')
    actions.add_parser('list', help='List managed directories (read only)')
    add = actions.add_parser('add', help='Add a source directory')
    add.add_argument('path')
    add.add_argument('--yes', action='store_true', help='Confirm for automation')
    remove = actions.add_parser('remove', help='Remove a source profile (keeps backup data)')
    remove.add_argument('path')
    remove.add_argument('--yes', action='store_true', help='Confirm for automation')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == 'dirs':
        if args.dirs_action == 'list':
            _format_profiles(load_manifest())
            return 0
        if args.dirs_action == 'init':
            return _print_management_result(init_directories(args.yes))
        if args.dirs_action == 'add':
            return _print_management_result(add_directory(args.path, args.yes))
        if args.dirs_action == 'remove':
            return _print_management_result(remove_directory(args.path, args.yes))

    # Keep the existing nonblocking lock shared by manual, watcher, and --check runs.
    try:
        with _run_lock():
            manifest_file = manifest_path()
            if manifest_file.exists():
                return _sync_manifest(load_manifest(), check=args.check)
            # Preserve the legacy read-only check API for pre-migration installations.
            if args.check:
                validate_mounts()
                load_config()
                print('PASS: source disk, TelDrive mount, paths, mirror and versioning configuration.')
                return 0
            print('No managed directory manifest; run `freefilesync-backup dirs init` explicitly.')
            return 0
    except RuntimeError as error:
        if str(error) == 'Backup or directory management is already running':
            print('Backup already running; no overlapping sync started.', flush=True)
            return 75
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ET.ParseError, KeyError, AttributeError) as error:
        print(f'Backup refused/failed: {error}', flush=True)
        raise SystemExit(2)
