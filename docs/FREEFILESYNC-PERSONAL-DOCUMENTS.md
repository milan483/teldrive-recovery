# Personal Documents: FreeFileSync operation

Configured 2026-11-20 with user approval for the initial upload and login startup.

- Source: `/mnt/HDD1/Personal Documents/` (approximately 1.4 GiB allocated locally).
- Latest backup: `/home/kylan/Mounts/teldrive-crypt/Backups/Personal Documents/current/`.
- History: sibling `versioning/<unique-run-id>/<FreeFileSync timestamp>/`.
- Mode: one-way mirror. Replaced and deleted destination files are archived.
- Comparison: time and size, not full content checksums. Symlinks are excluded.
- Trigger: RealTimeSync watches **only the configured sources**, after **300 seconds (5 minutes)
  without detected changes**. It starts at desktop login, not before login.
- No automatic history pruning is enabled.

## Installed files

- `~/.config/FreeFileSync/Personal Documents.ffs_batch`: base batch configuration.
- `~/.config/FreeFileSync/Personal Documents.ffs_real`: generated watcher configuration.
- `~/.config/FreeFileSync/managed-directories.json`: private managed source list.
- `~/.config/autostart/freefilesync-personal-documents.desktop`: login startup.
- `~/.local/bin/freefilesync-personal-documents`: logged launcher.
- `~/.local/lib/freefilesync-personal-documents/freefilesync_backup.py`: installed
  copy of this repository's `freefilesync_backup.py`.

## Add and remove backup directories

Use the management command rather than editing folder pairs in FreeFileSync:

```bash
freefilesync-backup dirs list
freefilesync-backup dirs add "/mnt/HDD1/Photos/"
freefilesync-backup dirs remove "/mnt/HDD1/Photos/"
```

These examples do not add Photos automatically. The installed source list starts
with the existing Personal Documents entry only.

Add/remove operations ask for confirmation. Adding a source records its mounted
filesystem identity and assigns a separate `Backups/<folder-name>/current/` and
`versioning/` destination. Duplicate/nested sources and colliding destination
names are refused. Remote sources and paths overlapping the backup mount are
not permitted. Each source must be on its expected mounted filesystem when a
backup runs.

The current installation is migrated already. On a fresh installation of these
helpers, `freefilesync-backup dirs init` explicitly migrates the approved default
Personal Documents job before additions; listing never initializes configuration.
Migration retained only Personal Documents, performed no synchronization, and
restarted the existing owned watcher with a verified 300-second idle delay.

Removing a source only removes it from the managed job and watcher. It never
deletes its local files, existing backup, or version history, and it works even
if the source has disappeared. Removing every source disables useful monitoring
and makes backup execution a no-op; no existing backup is emptied.
Existing backup destinations are not automatically adopted when adding a source,
including after removal; reattaching preserved backup data requires a deliberate
configuration change rather than silently taking ownership of it.

Configuration changes share the backup lock, preserve a private previous
configuration, and refresh the source watcher with the same **300-second** idle
delay. An add does not immediately upload the new folder; use the guarded run
command when ready, or let a subsequent source change trigger the watcher.
Only an already-running watcher is restarted automatically. If it was stopped,
the command reports that fact; start RealTimeSync with the generated `.ffs_real`
file or log in again before expecting automatic triggers. Keep an eye on any
watcher-refresh warning: the source list is committed even if restart fails.

The management command prints diagnostics directly. The older
`freefilesync-personal-documents` launcher remains the logged unattended entry
point. Do not bypass these launchers by running a batch XML directly.

Run through the launcher, **not directly through the batch XML**. Direct GUI/batch
execution bypasses launcher mount checks, locking, and unique history run IDs.

```bash
# Read-only validation (writes local diagnostic log only)
~/.local/bin/freefilesync-personal-documents --check

# Run now; also the command executed by RealTimeSync
~/.local/bin/freefilesync-personal-documents
```

The launcher refuses the wrong source filesystem UUID, a missing/wrong TelDrive
mount, redirected paths, unexpected nested mounts, and changed mirror/versioning
settings. It takes a nonblocking lock, and uses fresh archive paths for every run.
These are preflight checks, not protection against mounts changing mid-operation.

## Status and stopping automation

- Launcher log: `~/.local/state/freefilesync-personal-documents/launcher.log`.
- Latest exit status: `~/.local/state/freefilesync-personal-documents/last-result.json`.
- FreeFileSync HTML logs: `~/.local/state/freefilesync-personal-documents/logs/`.
- FreeFileSync codes: 0 success, 1 warning, 2 error, 3 cancelled. Launcher code 75
  means another guarded run holds the lock.
- To stop monitoring now, exit the Personal Documents RealTimeSync tray instance.
- To disable future login startup, add `Hidden=true` to its autostart desktop file.

RealTimeSync does not watch changes while its command runs. Changes during sync
can wait until a later trigger; no periodic reconciliation or disconnected-mount
retry service has been added. A refused/skipped event does not guarantee a retry.
Use the guarded manual command after reconnecting storage. Never run a second
unguarded batch concurrently.

## Cloud upload and recovery

The mount uses rclone full VFS write-back caching. FreeFileSync exit 0 means the
mount accepted writes, **not necessarily that TelDrive uploaded them**. Inspect
`~/.local/share/teldrive/rclone-crypt-mount.log` for upload errors and verify native
remote readback before relying on the backup or removing originals/cache data.
`last-result.json` deliberately does not mark remote upload verified.

To restore, copy the desired ordinary file from `current/` or its archived path
to a separate local restore directory, then inspect it. Do not edit current/history
in place. This is file history, not a full dated directory snapshot. The existing
Restic backups and USB recovery kit remain unchanged and complementary.

The disposable compatibility test is documented in
[FREEFILESYNC-TELDRIVE-TEST.md](FREEFILESYNC-TELDRIVE-TEST.md). It verified all test
payloads through native remote readback; it is not verification of the entire
production upload. The first production attempt stopped without transferring
files because the destination did not exist; the approved destination directories
were then created and the initial run retried. The retry completed successfully
at `2026-11-20T10:36:26Z`, exit 0, zero errors and warnings: 9,860 processed items,
1,475,943,057 bytes, 324 seconds. This is mount-level completion; full production
remote upload/readback remains unverified. Logs also recorded one overlapping
launch being refused, confirming the live lock prevented concurrent execution.

## Checks

```bash
python3 -m unittest -v test_freefilesync_backup.py
python3 freefilesync_backup.py --check
```
