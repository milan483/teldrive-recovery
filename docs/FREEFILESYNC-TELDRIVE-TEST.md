# FreeFileSync versioning test on TelDrive

Test date: 2026-11-20. FreeFileSync package: `14.13-3`.

## Scope

The user authorized disposable test uploads only. Personal Documents was not
synchronized, and no production job, RealTimeSync watcher, or autostart was
enabled. The existing Restic configuration was not changed.

The test used the existing `teldrive-crypt:` rclone FUSE mount at
`/home/kylan/Mounts/teldrive-crypt`, with mirror directions, time-and-size
comparison, Versioning deletion policy, and `TimeStamp-Folder` history.
The archive and current directories were separate siblings.

## Results

Three FreeFileSync batch runs returned exit 0 with zero errors and warnings:

1. Initial copy: three files, 49 bytes.
2. Update: replaced one file, deleted one source file, and added one new file.
   The old replaced file and the deleted destination file were both saved in
   `versioning/2026-11-20 182608/`. The unchanged file was not archived.
3. Repeat without changes: zero items and zero bytes processed.

Assertions verified current file contents, deleted-file absence from current,
archived contents, and recovery of both archived files to separate local files.
Source content remained unchanged by synchronization.

Direct `rclone-teldrive lsf` listed exactly the five expected current/archived
files. Separate `rclone-teldrive cat` calls read back all five from the native
`teldrive-crypt:` remote, bypassing the FUSE cache, and each matched its expected
bytes. This confirms remote readback for these fixtures, not only local cache
visibility.

## Retained evidence

- Local fixture, batch XML, stdout/stderr, HTML logs, and restored files:
  `/tmp/opencode/ffs-teldrive-test-43c0rru7/`.
- Remote fixture:
  `teldrive-crypt:ffs-versioning-test-b189f3feb2fc4f02a8a33f94f5788b18/`.
- Test driver: `/tmp/opencode/test_ffs_teldrive.py`.
- An earlier attempt was terminated by a one-second command timeout. Its
  fixture directories were retained at `/tmp/opencode/ffs-teldrive-test-e231qkyx/`
  and `teldrive-crypt:ffs-versioning-test-a93a50170ade41a18f0e43d7176788ac/`.
  That interrupted attempt is not counted as a successful test.

No remote test directories were deleted.

## Limitations

The mount uses `--vfs-cache-mode full`, so successful filesystem operations alone
do not establish completed remote uploads. Direct backend readback was performed
for all five expected files in addition to the mount-level checks.

This small test does not establish failure safety during disconnection, cache
exhaustion, simultaneous runs, timestamp collisions, large-file uploads, or
recovery after loss of the local cache. RealTimeSync triggering and production
mount guards remain unconfigured and untested. It is not a complete historical
directory restore test.

Before enabling production, confirm mount guards, non-overlap locking, idle
delay, logs, and initial full-upload approval. Cloud write-back errors must be
monitored separately from FreeFileSync's own success status.
