# Phase 1: Versioned file backups through TelDrive

This optional file-backup tool complements `backup-teldrive.sh`. It does **not**
replace the separate USB recovery kit for TelDrive's database, configuration,
API keys, and rclone crypt keys. It is a local Python CLI that asks Restic to
write snapshots using the custom `rclone-teldrive` binary.

## Scope

- Configure named backup profiles with selected local directories and excludes.
- Initialize one encrypted Restic repository on the existing TelDrive remote.
- Manually back up a profile with content-level deduplication and version history.
- List only snapshots belonging to a given profile.
- Restore a selected version to a new or empty directory, never over source files.
- Apply an optional basic rclone bandwidth limit.
- Reject missing sources, blocked TelDrive mount sources, insecure password
  files, overlapping source paths, and ambiguous snapshot selections.
- Optionally require specific source filesystems to be mounted before backing up.
- Display a sanitized numeric transfer estimate during dry runs and safe error
  categories on failure without revealing raw backend output.

Not included yet: automatic scheduling, KDE notifications, retention/pruning,
daily transfer budgets, metered-network detection, GUI, or unattended recovery
kit encryption. Do not expect the tool to perform any of those tasks.

## Requirements

- CachyOS/Arch Linux with Python 3.11+ (`tomllib`, `fcntl`), Restic 0.17+ (for
  `restore --overwrite never`), and the
  custom executable from `milan483/rclone-teldrive-v2`.
- Working TelDrive server and `teldrive-crypt:` rclone remote. Confirm:

  ```bash
  ~/.local/bin/rclone-teldrive lsd teldrive-crypt:
  ```

- A current, independently stored `teldrive-recovery` archive and its password.
- Enough local space for temporary file processing and Restic's cache.

**Compatibility warning:** Local encrypted transport is tested with Restic 0.20.0
and the installed custom rclone v1.76.0-DEV. This is not a guarantee for every
TelDrive server/fork version. Test with a tiny disposable profile before trusting real data.
Do not point the test repository at existing backup files.

## Install and configure

Run from the `teldrive-recovery` repository after applying the Phase 1 files:

```bash
sudo pacman -S --needed restic python
mkdir -p ~/.config/teldrive-backup
install -m 600 examples/teldrive-backup.toml ~/.config/teldrive-backup/config.toml
```

Edit the config to select existing directories and confirm the remote name,
binary path, dedicated repository directory, and your TelDrive mount path.
Keep separate data sets in distinct profiles. A missing source path causes the
entire profile to fail before starting a backup rather than silently omitting it.

```bash
python3 teldrive_backup.py profiles
python3 teldrive_backup.py keygen
python3 teldrive_backup.py init
```

`keygen` creates a strong Restic password file in mode 0600 and refuses to
overwrite one. **Back up that password separately before relying on snapshots.**
It must survive loss of your PC. `init` is intended to run **once** against a
new, dedicated repository path. An existing repository must keep its original
password file; never generate a new password for it. The tool passes the
password file path through `RESTIC_PASSWORD_FILE`, not as a command-line
password argument.

The Restic password and the rclone crypt password/salt are different secrets.
You need both if the repository is stored through `teldrive-crypt:`. Store a
separate copy of the TelDrive recovery kit, which includes the server database
and access material, somewhere other than TelDrive.

## Back up a profile and browse versions

To manage selected directories without editing TOML, use `teldrive-backup dirs`
for an interactive menu, or `teldrive-backup dirs add PROFILE "/absolute/path"`
and `teldrive-backup dirs remove PROFILE "/absolute/path"`. Changes require
confirmation and preserve a private previous-config copy. Removing sources does
not delete snapshots or source files. See [the overview](OVERVIEW.md#6-configuration-and-maintenance)
for details.

```bash
python3 teldrive_backup.py run documents --dry-run
python3 teldrive_backup.py run documents
python3 teldrive_backup.py snapshots documents
```

Each snapshot receives a `teldrive-backup:<profile>` tag. Multiple profiles
share a Restic repository, allowing reuse of data across profiles; history is
filtered by tag. Each profile is still explicitly selected when running a
backup. Backups do not delete source files or automatically expire snapshots.

`one_file_system = true` prevents walking into other mounted filesystems from
the selected source, but may also exclude nested Btrfs subvolumes. Add such
subvolumes as explicit source entries if they should be included. Additionally,
the tool blocks selected sources located inside the configured TelDrive mount.
Active rclone FUSE mounts are also detected through Linux `/proc/self/mountinfo`:
sources inside them or containing them are refused, even if they are unlisted or
`one_file_system = false`. All rclone mounts are blocked conservatively because
crypt/alias remotes can hide TelDrive. Configured blocked mounts are excluded from
broad sources even while unmounted. Keep those paths configured; do not mount a
new remote or replace source paths while a backup is running. This tool does not
freeze the filesystem; application-consistent database backups need separate tooling.
An unplugged source whose mountpoint still exists as an empty directory cannot
be distinguished from an intentionally empty source unless `required_mounts`
is configured. For a profile whose source is `/mnt/HDD1/Documents`, set
`required_mounts = ["/mnt/HDD1"]` in that profile's `[[profiles]]` block. The
tool checks Linux mountinfo (including bind mounts) immediately before backup;
it cancels if the required filesystem is absent. Every required mount must
contain at least one of that profile's source paths. For removable data sets,
set this explicitly even if the path usually exists while unplugged. Newly
added sources are **not** automatically assigned a required mount.

## Restore a version safely

### Browse without restoring everything

Run `teldrive-backup browse` (or `python3 teldrive_backup.py browse`) in a
terminal. This mounts snapshots read-only at `~/Mounts/teldrive-backups` using
Restic FUSE. Keep that terminal open and open the directory in Dolphin. Browse
`snapshots/` for dated versions or `tags/` for profile-tagged versions; original
absolute source paths appear underneath each snapshot. Files download on demand.
No files appear until the first backup has completed. An existing mount may need
to be unmounted and restarted to show newly created snapshots.

To stop it from another terminal:

```bash
fusermount -u "$HOME/Mounts/teldrive-backups"
```

Use `browse --target /absolute/empty/local/directory` for a different location.
The target must be empty, unmounted, outside configured sources and remote mounts,
and have no symlink parents. FUSE and a working `fusermount` helper are required.
Nothing is scheduled or mounted at login. This is a foreground, manual command.
Copy files out to recover them; you cannot edit the backup through this mount.

### Restore to local disk

Choose a full or unambiguous snapshot ID from `snapshots`:

```bash
python3 teldrive_backup.py restore documents latest \
  --target ~/Restored-documents-2026-11-20
```

The tool resolves `latest` within the selected profile, verifies the snapshot
tag, and requires an empty or nonexistent target outside **all** configured source
directories and configured/active blocked mounts. Symlink targets and symlink
parents are refused. The target is checked again after snapshot lookup under the
local operation lock, and `--overwrite never` provides additional file protection.
Restic restores the original
directory structure underneath the target. Inspect the restored paths and
compare files before copying them anywhere else.

If a restore is interrupted, the target may contain partial data. Keep that
directory for diagnosis and restore again to a different empty target. This
tool never automatically deletes a partial restore. Use a private local parent
directory, and do not let another process modify the target or its ancestors while
restoring: the local lock coordinates this tool, not arbitrary filesystem writers.

## Credentials and error reporting

Use a named remote in a private rclone configuration, not inline connection strings
or credentials in the repository URL. Password-file symlinks are refused; keep the
password and configuration directories private. The tool suppresses child stdout,
and rclone logging rather than risk backend errors containing tokens. It collects
child stderr privately for classification and prints only a predefined error
category and exit code; raw messages, URLs, paths, tokens and headers are never
reprinted. It only announces completion on success. Snapshot listings are parsed
and validated, not raw logs. There is intentionally no per-file progress display.
Dry-run summaries are parsed from documented Restic JSON numeric fields.
Do not enable shell tracing,
HTTP dumping, or external credential logging. A same-user/root attacker or a
malicious executable/configuration is outside this tool's security boundary.

Restic's normal repository locks remain enabled. Interrupted uploads can leave
unreferenced data or stale locks; valid earlier snapshots are not forgotten or
pruned. This tool never automatically unlocks, repairs or deletes repository data.

## Bandwidth and integrity

An optional `bandwidth_limit = "1M"` sets `RCLONE_BWLIMIT`, so the Restic
transport is throttled to approximately 1 MiB/s. This is **not** 1 Mbps.
Restic deduplicates unchanged content, but maintenance and metadata operations
can still use network bandwidth. There is no daily byte cap yet.
The `run PROFILE --dry-run` command now prints new/changed/unchanged file counts
and estimated compressed/uncompressed added data. These are estimates of new
repository data, **not exact on-the-wire bytes**; protocol overhead and retries
can change actual bandwidth consumption. If the version of Restic returns no
valid structured summary, the estimate is reported as unknown, never zero.

The initial backup may upload nearly the entire selected data set. Run small
test backups first. Verify these scenarios manually on a *disposable* remote
repository before backing up important data:

1. Upload a tiny directory; list its snapshot and restore it into a new path.
2. Repeat with no source changes; confirm minimal new transferred data.
3. Modify one file; confirm the original and newer versions both restore.
4. Delete a source file; confirm the old snapshot can still restore it.
5. Disconnect the remote, interrupt an upload, and retry; confirm no valid
   earlier snapshot disappears.
6. Try a missing source or disconnected local drive; confirm the profile fails.

For stronger checks, use Restic's standard `check` and periodic restore tests
after validating the custom backend. Full data scans can download significant
volumes from TelDrive. Phase 4 will provide managed verification and retention.

## Tests

These are offline tests using temporary files and mocks; they do not require
Telegram access and cannot establish that real backups or restores work:

```bash
python3 -m py_compile teldrive_backup.py test_teldrive_backup.py
python3 -m unittest -v test_teldrive_backup.py
bash -n backup-teldrive.sh restore-teldrive.sh
python3 test_restore_teldrive.py
```

Optional real transport integration (uses only a fresh local crypt remote, never
your rclone config or TelDrive):

```bash
TELDRIVE_TEST_RCLONE="$HOME/.local/bin/rclone-teldrive" \
  python3 -m unittest -v test_teldrive_backup_integration.py
```

Without Restic on PATH and an explicit binary, that test is skipped. It covers
initial/repeated/modified backups, zero new file-data blobs for unchanged content,
profile isolation, old/latest restores, missing sources, existing-target refusal,
and `check --read-data`.

### Validation on 2026-11-20

With explicit user approval, a live smoke test also passed against the installed
TelDrive v2 backend using Restic 0.20.0 and custom rclone v1.76.0-DEV. A new,
isolated repository was used:
`teldrive-crypt:backups/phase1-smoke-3ef23cdd9915b24cbe61e430`.
Initial/unchanged/modified backups, profile isolation, old/latest restores,
nonempty-target refusal, missing-source refusal and `check --read-data` passed.
The unchanged backup added zero new file-data blobs. Remote test data was retained;
no preexisting repository was initialized or modified. Its local key/config remain
outside Git under `/tmp/opencode/teldrive-phase1-live-0qj_fj5f/`.
Live network interruption, power loss, large multipart uploads, Telegram account
recovery and replacement-computer recovery were **not** exercised by this test.

## Upstream references

- Restic rclone backend: <https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html>
- Restic snapshots and content deduplication: <https://restic.readthedocs.io/en/stable/040_backup.html>
- Rclone's Restic server: <https://rclone.org/commands/rclone_serve_restic/>
