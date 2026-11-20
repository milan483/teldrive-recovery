# TelDrive backup: operation and maintenance overview

This is the operator guide for Kylan's current installation. For configuration
details and safety boundaries, see [BACKUP-WORKFLOW.md](BACKUP-WORKFLOW.md).

## 1. What this setup does

There are two complementary kinds of backup:

1. **File backups:** Restic saves encrypted, versioned snapshots of explicitly
   selected local directories into TelDrive through the custom rclone backend.
   Repeated backups reuse unchanged content. Older snapshots remain available
   when a source file changes or is deleted.
2. **TelDrive recovery kit:** `backup-teldrive.sh` creates an encrypted USB archive
   of TelDrive's database, configuration, keys, custom binary, and recovery
   scripts. It recovers access to TelDrive; it is not a copy of your documents.

The file-backup data path is:

```text
Selected local directory
  -> Restic encryption, deduplication and snapshot history
  -> custom rclone-teldrive -> teldrive-crypt remote
  -> TelDrive server -> Telegram storage
```

Backups access the remote directly; they do not write through your desktop mount.
Restic protects the backup contents, while the rclone crypt layer also encrypts
the remote representation. Recovery requires both sets of access material.

**Phase 2 adds optional automation, but does not turn it on by default.** You
can explicitly enable per-profile user-systemd timers, network retries and KDE
notifications, and a separate age-encrypted recovery-kit timer. It does not
introduce retention, pruning, data deletion, or transfer budgets. The existing
TelDrive server/mount services remain separate from backup execution.

## 2. Your directories and installed files

| Purpose | Location |
| --- | --- |
| `documents` source | `/mnt/HDD1/Personal Documents/` |
| `coding` source | `/mnt/HDD1/Coding/` |
| `pictures` source | `/home/kylan/Pictures/` (currently resolves to `/mnt/HDD3/home/Pictures/`) |
| Restic repository | `rclone:teldrive-crypt:backups/restic-cachyos` |
| Existing normal TelDrive mount | `/home/kylan/Mounts/teldrive-crypt/` |
| Read-only backup browse mount | `/home/kylan/Mounts/teldrive-backups/` |
| Backup CLI | `~/.local/bin/teldrive-backup` |
| Restic binary | `~/.local/bin/restic` |
| Custom rclone binary | `~/.local/bin/rclone-teldrive` |
| Active backup configuration | `~/.config/teldrive-backup/config.toml` |
| Restic password file | `~/.config/teldrive-backup/restic-password` |
| Configuration template | `~/.config/teldrive-backup/config.toml.example` |
| Local checkout with `main` | `/mnt/HDD1/Coding/Projects/teldrive-recovery/` |
| Installed workflow instructions | `~/.local/share/doc/teldrive-backup/BACKUP-WORKFLOW.md` |

All three profiles currently have no filename exclusions. Nested filesystems
are not traversed automatically (`one_file_system = true`). Do not assume that
every subvolume underneath a selected directory is included.

The repository folder appears in the normal TelDrive mount under
`backups/restic-cachyos/`, but contains **Restic's internal encrypted objects**,
not ordinary documents. Never edit, move, sync over, or delete those objects.
Use the separate backup browse mount to see your actual files.

At setup handoff on 2026-11-20, the production repository was initialized but the
three profiles had not been backed up. Use `snapshots` below for current status;
repository initialization alone does not protect any source files.

## 3. Everyday controls

Run commands as your normal user, not with `sudo`.

```bash
# Show configured source profiles.
teldrive-backup profiles

# Back up just one profile.
teldrive-backup run documents
teldrive-backup run coding
teldrive-backup run pictures

# Or run all three sequentially, stopping on a failure.
teldrive-backup run documents &&
teldrive-backup run coding &&
teldrive-backup run pictures

# Check a backup operation without creating a snapshot.
teldrive-backup run coding --dry-run

# After configuring a profile's `schedule` and validating a real backup:
teldrive-backup schedule list
teldrive-backup schedule enable coding
# Disable later: teldrive-backup schedule disable coding

# List each profile's saved versions.
teldrive-backup snapshots documents
teldrive-backup snapshots coding
teldrive-backup snapshots pictures

# Show command help.
teldrive-backup --help
```

The current bandwidth limit is **1 MiB/s**, not 1 megabit/s. An initial upload
can take a long time. Keep the terminal session and computer running. There is
no live progress display: backend output is suppressed to avoid exposing tokens
in error messages. Commands announce success only after Restic succeeds.
Unchanged file data is deduplicated, although metadata and network operations
can still consume bandwidth.

Press **Ctrl+C** to interrupt a foreground operation. Earlier completed
snapshots remain; the interrupted run may leave partial data or a stale lock.
Do not assume an interrupted or failed run created a complete backup.

## 4. Browse backups normally in Dolphin

Start the read-only browse mount in a terminal:

```bash
teldrive-backup browse
```

Keep that terminal open. In Dolphin, enter:

```text
/home/kylan/Mounts/teldrive-backups/
```

- `snapshots/` shows dated versions.
- `tags/` groups snapshots under tags such as `teldrive-backup:documents`.
- `ids/` shows snapshot IDs.
- Source paths are preserved inside each snapshot. For example, documents
  appear under `mnt/HDD1/Personal Documents/` within a snapshot.

Open or copy files as usual. Files are downloaded when needed, so browsing
requires a working server/network. You cannot modify the backup through this
mount. An empty repository has no file snapshots to browse.

Unmount from another terminal:

```bash
fusermount -u "$HOME/Mounts/teldrive-backups"
```

If it is busy, close files and Dolphin tabs using the mount, and move any shell
working directories outside it before trying again. Do not force-unmount it as
a routine workaround. Unmount and restart `browse` to refresh newly added
snapshots. The browse mount does not start automatically at login.

## 5. Restore a version to local disk

```bash
# Restore the newest documents snapshot.
teldrive-backup restore documents latest \
  --target /mnt/HDD1/Restored-documents

# Or replace SNAPSHOT_ID with an ID from the profile's snapshot listing.
teldrive-backup restore documents SNAPSHOT_ID \
  --target /mnt/HDD1/Restored-documents-older
```

The target must be new or empty, outside all configured sources and remote
mounts, with no symlink parents. Original source paths are recreated beneath it.
Inspect the result before manually copying anything over current working files.
Ensure enough local free space first. For one or two files, copying from the
read-only browse mount may be more convenient.

An interrupted restore leaves its partial output intact. Use a different empty
target for the next attempt. The tool does not erase or repair partial restores.

## 6. Configuration and maintenance

### Add or remove directories with the helper

Open the interactive menu:

```bash
teldrive-backup dirs
```

Choose add/remove, enter the profile name and directory path, then confirm.
Paths entered at the prompt can contain spaces; do not wrap them in quotes.
Adding to a new profile name creates an independently controlled profile.

You can also specify the change directly (shell paths containing spaces need quotes):

```bash
teldrive-backup dirs list
teldrive-backup dirs add documents "/mnt/HDD1/Another Folder"
teldrive-backup dirs add music "/mnt/HDD1/Music"
teldrive-backup dirs remove music "/mnt/HDD1/Music"
```

These commands ask before saving. `--yes` skips confirmation for an intentional
noninteractive change. No backup starts and no local or remote data is deleted.
Removing the last source in a profile removes that profile from the config;
its snapshots remain. Re-add the same profile name to access its history again.
Removing the final source of the final profile is refused, since the tool needs
at least one configured source. Missing paths can be removed, but additions must
exist and pass mount/overlap safety checks.

Each saved edit creates a private, uniquely named
`~/.config/teldrive-backup/config.toml.before-dirs-*` copy of the previous config.
The new config is written atomically with private permissions. Settings are
preserved, but comments/formatting are normalized. Existing backup snapshots and
repository/password settings are not changed. Config copies accumulate; keep them
private and review them manually rather than deleting them automatically.

### Edit other settings manually

Edit configuration with:

```bash
nano ~/.config/teldrive-backup/config.toml
```

Changes apply to the next command; no backup service needs restarting.

- **Sources:** change the `sources` list only deliberately. Verify new paths with
  `profiles`, then a dry run. Missing directories fail the operation. For sources
  on removable HDDs or USB drives, add `required_mounts = ["/mnt/HDD1"]` to the
  relevant `[[profiles]]` block (using the real mountpoint) so an unplugged drive
  cannot produce a misleading empty backup. The `dirs` menu does not infer it.
- **Profiles:** keep names stable if you want continuous profile history. History
  is selected by `teldrive-backup:<name>` tags; renaming a profile does not retag
  existing snapshots.
- **Exclusions:** add Restic exclusion patterns to a profile's `excludes` list
  only for data you intentionally do not want protected. Coding backups currently
  include dependencies, build outputs, `.git` directories and local secrets if
  present; review size and sensitivity before the first upload.
- **Bandwidth:** `bandwidth_limit = "1M"` limits transfers to approximately
  1 MiB/s; `"0"` means unlimited. A dry run reports estimated added repository
  bytes and file counts, not guaranteed on-the-wire transfer totals.
- **Mount protection:** keep all known TelDrive mount paths in
  `safety.blocked_sources`. Active rclone and Restic browse mounts are also
  detected. Do not mount new remotes or replace paths during an operation.
- **Repository/password:** do not change the repository URL or regenerate its
  password as ordinary maintenance. A new password file does not unlock an
  existing repository.
- **Capacity:** backups retain history indefinitely. Monitor storage and local
  cache usage. Do not manually delete repository objects to reclaim space.
- **Verification:** regularly list snapshots, browse important files, and restore
  samples into a new directory. A successful upload is not a recovery rehearsal.
- **Updates:** the Restic binary was installed locally, not through Pacman.
  Review updates to Restic/custom rclone, rerun tests, and verify a sample restore.
  Source edits do not automatically update `~/.local/bin/teldrive-backup` or the
  installed documentation.

For advanced integrity verification, Restic supports `check` and
`check --read-data`. These are not CLI subcommands of `teldrive-backup`; use the
same repository, password-file environment and custom rclone transport when
running Restic directly. Full data checks download repository contents and can
be expensive. Never improvise `forget`, `prune`, `repair` or `unlock` commands on
the production repository.

## 7. Protect recovery material

Keep separately recoverable copies of:

- The **Restic password**, outside this computer and outside TelDrive.
- The **TelDrive recovery kit** and its separate archive password. It includes
  database/configuration and rclone crypt/access material.
- Telegram account access and any required 2FA/recovery information.

The existing USB recovery-kit procedure is documented in [the README](../README.md).
It is separate from Restic and should be renewed after relevant deployment or key
changes. The existing recovery-kit script does **not** automatically include the
new Restic password/configuration, so preserve those separately.

Keep password/configuration files private. Never paste their contents into logs,
Git, screenshots or support requests. Avoid shell tracing and HTTP debug dumps.
Do not put the only recovery copy inside the repository it is needed to unlock.

TelDrive/Telegram remains one storage dependency. Keep an independent offline or
second-provider copy for irreplaceable data; snapshots on the same remote do not
protect against losing access to that remote.

## 8. Troubleshooting

| Symptom | Safe first action |
| --- | --- |
| `teldrive-backup: command not found` | Use `~/.local/bin/teldrive-backup`; ensure `~/.local/bin` is on PATH. |
| Missing source | Verify the source path and drive mount. Do not remove a source from config just to make the error disappear. |
| Blocked mount/source overlap | Select actual local data, not TelDrive or the backup browse mount. |
| No snapshots | Check the selected profile and whether its first successful backup completed. |
| Browse target already mounted/nonempty | Use the existing mount, unmount it normally, or choose a different empty local target. Never delete unknown contents. |
| Restic failure code | Check server/network, password-file availability, disk space and whether another operation is running. Backend details are intentionally suppressed. |
| Repository locked | Let active operations finish. A browse mount can also hold a lock. Unmount it and check for running Restic processes. Seek help before removing a stale lock. |
| Restore destination rejected | Pick a new empty local directory outside the configured sources. |

Read-only status commands:

```bash
systemctl --user status teldrive-server.service teldrive-mount.service
findmnt --mountpoint "$HOME/Mounts/teldrive-backups"
pgrep -a restic
```

The existing `teldrive-mount.service` controls the normal remote mount, **not**
the separate Restic browse mount. Stopping the server while backing up or browsing
will disrupt remote access. Do not stop/restart it as a first response to a long
initial upload.

## 9. Validation and implementation status

Phase 1 was validated with Restic 0.20.0 and custom rclone v1.76.0-DEV. An approved
disposable live repository passed small backup/unchanged-backup/restore and full
integrity checks. That is not a production-scale or disaster-recovery rehearsal.
Live power loss, network interruption and large multipart uploads remain unverified.

The browse and directory-management additions passed 60 automated tests, Ruff and Python syntax checks;
the production repository mounted successfully with read-only FUSE options.
Source updates are not automatically installed. Use Git status and compare the
installed CLI with the checkout rather than assuming the installed copy matches
`main`.

For development validation from the checkout:

```bash
python3 -m unittest discover -v
python3 -m py_compile teldrive_backup.py test_teldrive_backup.py \
  test_teldrive_backup_integration.py test_restore_teldrive.py
bash -n backup-teldrive.sh restore-teldrive.sh restore.sh
```

The local real-transport integration test is skipped unless Restic is on PATH and
`TELDRIVE_TEST_RCLONE` explicitly points to the custom binary. See the workflow
guide for enabling it. Do not rerun live remote initialization as a test against
the production repository.
