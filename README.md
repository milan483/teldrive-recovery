# TelDrive recovery kit

Personal backup and restore helpers for a local Podman TelDrive server and an
rclone crypt mount on CachyOS/Arch Linux x86-64.

## Back up

Keep `backup-teldrive.sh`, `restore.sh`, and `restore-teldrive.sh` together. With the USB mounted:

```bash
bash backup-teldrive.sh
```

The default destination is `/run/media/kylan/netac/Obsidian USB`. An optional
first argument changes the output directory. Use `--require-mount /mount/path`
to specify a different real mountpoint (the destination must be inside it).

Enter a strong non-empty password at the 7-Zip prompts, then enter it again to
test the archive. Store that password separately from the USB.

Requires `7z`, Python 3, Podman/Compose and the existing local deployment under
`~/.local/share/teldrive`. The archive includes a PostgreSQL dump, server secrets,
all configured rclone remotes, crypt recovery keys, the custom rclone binary,
service files, checksums, and an automatic restore script. Container images are
pinned to the running image digests.

## Versioned and scheduled file backups (optional Phases 1–2)

Start with [the operation and maintenance overview](docs/OVERVIEW.md) for this
machine's profiles, everyday commands, read-only browsing, restores and recovery
precautions.

See [the backup workflow](docs/BACKUP-WORKFLOW.md) for selected directory
profiles, encrypted Restic snapshots through the custom TelDrive rclone backend,
safe restores, and opt-in per-profile user-systemd scheduling with bounded
network retries and optional KDE notifications. A separate optional timer can
create recovery kits encrypted with an offline `age` recipient. Neither timer
is installed/enabled until you explicitly enable it. No pruning is enabled.

## Restore on a replacement computer

1. For `.7z` archives use 7-Zip and the password. For `.tar.age` archives
   decrypt with your offline age identity and extract the tar privately.
2. Open a terminal in the extracted folder.
3. Run `bash restore.sh` as your normal desktop user, not with sudo.
4. Approve missing-package installation if needed.
5. Open a known file in the resulting Dolphin mount to verify decryption.

The restore script checks archive checksums, refuses existing installation
paths and database volumes, restores PostgreSQL before starting TelDrive, and
enables the encrypted mount. Internet access is required; Telegram may require
reauthentication. A failed restore retains its data for diagnosis and is not
automatically resumable.

If a restore stops after creating a partial database volume, do not manually
guess the volume name. On this fresh recovery computer run:

```bash
bash restore.sh --cleanup
bash restore.sh
```

`--cleanup` lists the detected TelDrive containers, volumes, and paths, asks for
confirmation, and removes only that partial state. It refuses to proceed unless
you explicitly confirm. Never use it on a working installation.

If a restore is interrupted before completion on a genuinely fresh computer,
remove only the partial paths listed by the error, then rerun the script:

```bash
rm -rf ~/.local/share/teldrive ~/.local/bin/rclone-teldrive
rm -f ~/.config/rclone/rclone.conf
rm -f ~/.config/systemd/user/teldrive-server.service
rm -f ~/.config/systemd/user/teldrive-mount.service
```

Use those cleanup commands only when this computer has no existing TelDrive or
rclone setup you need to preserve. The restore script also normalizes the old
short `postgres:18-alpine` image name for strict rootless Podman installations.

## Limits and security

- This is a recovery kit for access to Telegram-hosted files, **not an independent
  backup of file contents**. Keep a separate payload backup for important data.
- Archive integrity checks do not establish a successful database or file restore.
  A full replacement-computer restore rehearsal is still required.
- Extracted archives contain plaintext credentials, including other rclone
  remotes. Protect the extraction directory and remove it when finished.
- Plaintext staging is removed on ordinary script exit, not securely erased;
  use encrypted local storage. A forced kill or power loss can leave staging.
- Never publish backup archives, `.env`, `rclone.conf`, database dumps, or recovery
  credentials to this repository, even though it is private.

## Checks

```bash
bash -n backup-teldrive.sh restore-teldrive.sh
python3 test_restore_teldrive.py
python3 -m unittest -q test_teldrive_backup.py test_teldrive_phase2.py
```

The tests validate kit checksums and overwrite protection without starting
containers or modifying a real installation.
