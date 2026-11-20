# Research: browsable incremental copies with separate version history

Researched 2026-11-20. This is a design note, not an installed backup workflow.

## Requirement and recommendation

Keep ordinary files in a browsable `current/` directory; copy new/modified source
files on subsequent runs, moving superseded destination files into a separate
`versioning/<unique-run-id>/` hierarchy. **For a local disk or an rsync-capable
SSH destination, rsync `--backup --backup-dir` is the simplest fit.** Its backup
directory preserves the original relative hierarchy of replaced/deleted files.
Use a new archive directory for every invocation, including retries. [1]

This differs from this repository's existing encrypted Restic object repository,
which requires Restic restore or a browse mount to expose documents. Phase 1 is
manual and has no scheduling/pruning. Nothing here replaces or modifies it or
the USB recovery kit; never point these copying tools at its repository objects.
See [README](../README.md), [workflow](BACKUP-WORKFLOW.md), and
[operator overview](OVERVIEW.md).

## Rsync design and safe preview

Illustrative, disjoint local paths (not this machine's configured paths):

```text
/data/source/                              original documents
/backup/documents/current/                 ordinary latest copied files
/backup/documents/versioning/<run-id>/      displaced old files only
```

Keep the archive an **absolute sibling of `current/`, outside both source and
destination**; keep both backup trees outside the source. Resolve symlinks and
mount identities before approving paths. Rsync interprets a relative
`--backup-dir` relative to the destination; nesting archives inside the copied
tree creates avoidable recursion/deletion hazards. On an SSH transfer the archive
path is on the receiving host. [1]

Preview only, using placeholder paths and a fresh, collision-checked run ID:

```bash
rsync -a --dry-run --itemize-changes --stats \
  --backup --backup-dir='/backup/documents/versioning/20261120T153000Z-run001' \
  -- '/data/source/' '/backup/documents/current/'
```

- `--dry-run` reports intended changes without copying or moving files. Review
  paths and itemized changes; a preview does not prove a later run will succeed.
  Only after approval would the real operation omit `--dry-run`. [1]
- The source **trailing slash** means copy its contents, not another `source/`
  level. `-a` preserves common metadata/symlinks but does not include ACLs,
  xattrs or hard-link preservation (`-A`, `-X`, `-H`); filesystem capabilities
  and permissions must be considered before adding these. [1]
- New files go to `current/`; old destination content displaced by an update goes
  to that run's archive. Unchanged content is not recopied/archived. A timestamp
  identifies the run that displaced a version, not the time it was created.
  Never reuse a daily archive path for multiple runs: repeated identical paths
  can replace an earlier saved version. [1]
- Default change detection is **size + modification time**, not content. A
  same-size edit with preserved mtime can be missed. `--checksum` compares file
  contents instead, with substantial extra read I/O on both sides; this is
  distinct from rsync's normal checksum verification of transferred files. [1]
- Incremental file selection does not mean block-delta storage: local transfers
  default to **whole-file** copying for selected files. Rsync normally uses its
  delta-transfer algorithm across a real rsync network connection; both ends
  require rsync. Do not assume this optimization when writing to a mounted cloud
  filesystem, or prefer rsync over the TelDrive FUSE mount. [1]

### Deletion is a separate policy choice

Start **without `--delete`**: source deletions leave destination files in
`current/`, so it is not an exact mirror. If an exact current mirror is wanted,
explicitly opt into `--delete`; with backup mode, displaced destination files
are retained in that run's archive. Preview the exact deletion-enabled command
first. Check source/destination drives are really mounted, source identity and
non-overlap, and expected deletion volume. An unmounted source appearing empty
can otherwise cause mass removal from `current/`. Consider a deletion cap
(`--max-delete`), but it is not a transaction/rollback guarantee. Do not add
`--delete-excluded` or `--ignore-errors` casually. [1]

### History and consistency limitations

This layout is a **reverse-incremental-style collection of full old files**, not
dated full snapshots or deduplicated blocks. Each run folder contains only what
was displaced, and a newly created file has no prior version. Consequently,
reconstructing an entire earlier tree requires undoing later changes in reverse
order, including removing files introduced later. Archive folders alone do not
record those creations: maintain per-run manifests/logs and restore tooling if
point-in-time tree recovery matters. Simply overlaying one archive folder is not
a complete restore. Individual old files remain directly browsable. These are
design consequences of rsync's documented backup semantics. [1]

The whole operation is **not atomic**: interruption can leave a mixture of old
and new destination state. Even `--delay-updates` does not make a filesystem-wide
transaction. A changing source is not a stable snapshot; databases and other live
applications need quiescing, application-consistent exports or filesystem
snapshots. Avoid `--inplace` for this design. Keep successful-run records, review
nonzero exits, retain failed-run history, and rehearse restores before relying on
it. History consumes space and has no automatic retention in this proposal. [1]

## Alternatives and TelDrive relevance

| Option | Fit and caveats |
| --- | --- |
| **FreeFileSync + Versioning** | GUI-oriented fit. Versioning preserves overwritten/deleted files; **Time stamp [Folder]** keeps original names under dated folders, closely matching this layout. **Replace** keeps only the most recent version at a given path, so choose timestamped history for multiple versions. Select one-way update/mirror behavior deliberately. [2] |
| **RealTimeSync** | Companion watcher that triggers a command, usually a FreeFileSync batch job, after an idle interval or when a directory becomes available. It does not itself implement the copying/versioning. Monitoring pauses while the command runs; some filesystem/network drivers provide no notifications. Retain periodic reconciliation rather than assuming every event is protected. [3] |
| **rclone `copy` / `sync` + `--backup-dir`** | Better candidate for a native TelDrive remote than rsync through FUSE. `copy` skips identical files and does not remove destination-only files; `sync` additionally mirrors source deletions. `--backup-dir` moves overwritten/deleted files into their original relative hierarchy. Use a unique run folder. The archive **must use the same remote as the destination**, which must support **server-side move or copy**. Destination/archive overlap is forbidden unless excluded by filters; prefer disjoint siblings. [4][5] |
| **Syncthing versioning** | Appropriate for a receiving device in device-to-device sync, not a standalone same-machine directory-copy job. Versioning is per-folder/per-device, disabled by default, and archives changes **received from another device only**; it cannot preserve a file's previous content when edited locally on that receiver. A custom versions path should be on the same filesystem. [6] |

For a future TelDrive layout, conceptually use
`teldrive-crypt:backups/documents/current` and
`teldrive-crypt:backups/documents/versioning/<unique-run-id>` on the **same named
crypt remote**, separate from the Restic repository. Ordinary files would be
visible through the decrypted mount, not as plaintext in the underlying crypt
storage. **Compatibility remains unverified:** this repository uses a custom
rclone v1.76.0-DEV backend, and existing Restic transport validation does not
establish its backup-dir server-side move/copy behavior, checksum support,
metadata fidelity or failure safety. An approved disposable native-remote test
must cover overwrite/deletion archival and old-file recovery before choosing
this alternative. The upstream requirements above are not proof that the custom
backend satisfies them. [4][5]; [repository compatibility note](BACKUP-WORKFLOW.md#requirements).

## Automation choices (conceptual only)

For this Linux environment, prefer a user **systemd timer + oneshot service**
after the copying policy is approved. Calendar timers support missed-run
catch-up with `Persistent=true`; a timer does not restart an already-active
service. Manual invocations still need a shared non-overlap lock. Account for
user-manager lifetime, absent disks, errors, logs and notifications. [7]
RealTimeSync is the GUI/event-driven alternative; its notification limitations
make a periodic full comparison useful. No scheduler or watcher is configured
by this note. [3]

## Verification and remaining work

Disposable, local, Python-driven rsync assertions were run under `/tmp/opencode`,
with automatic temporary cleanup:

- Dry run changed nothing and created no archive.
- A real run archived a prior modified file, copied a new file, did not archive
  an unchanged file, and left the source intact.
- Source deletion was retained in `current/` by default; a later `--delete` run
  moved that destination file to its unique run archive.
- A same-size `AAAA` to `BBBB` edit with restored source `mtime_ns` was skipped
  by `-a`; `-ac` detected and copied it.
- Three successive versions of nested `nested/note.txt` were preserved in
  distinct run folders: version 1 under `run2/nested/note.txt`, version 2 under
  `run3/nested/note.txt`, and the latest under `current/nested/note.txt`.
- A repeated unchanged run produced empty `--itemize-changes` output and
  preserved the destination inode and mtime. The local rsync used for these
  checks reported `3.5.2-g41bbd895`.

No TelDrive backend, scheduler, interruption or full historical-tree restore was
tested. No real backup data was accessed or changed, software installed, or
configuration changed. Only this research note was added.

## Primary sources checked

1. [Official rsync manual](https://download.samba.org/pub/rsync/rsync.1): usage/trailing slash; `--backup`, `--backup-dir`, `--checksum`, `--whole-file`, `--dry-run`, `--delete`, `--max-delete`, `--archive`, `--delay-updates`, `--inplace`.
2. [FreeFileSync: File Versioning](https://freefilesync.org/manual.php?topic=versioning).
3. [FreeFileSync: RealTimeSync](https://freefilesync.org/manual.php?topic=realtimesync).
4. [Rclone: backup-dir](https://rclone.org/docs/#backup-dir-string): same-remote, server-side operation and overlap requirements.
5. [Rclone: copy](https://rclone.org/commands/rclone_copy/): comparison, non-deletion, directory contents, dry-run and distinction from sync.
6. [Syncthing: File Versioning](https://docs.syncthing.net/users/versioning.html).
7. [systemd.timer official manual](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html).
