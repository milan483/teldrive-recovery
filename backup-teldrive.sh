#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Create an encrypted, independently restorable TelDrive recovery archive.
# The archive password is entered interactively and is never written to disk.

readonly BACKUP_ROOT="${1:-/run/media/kylan/netac/Obsidian USB}"
readonly TELDRIVE_ROOT="${HOME}/.local/share/teldrive"
readonly RCLONE_CONFIG="${HOME}/.config/rclone/rclone.conf"
readonly CRYPT_RECOVERY="${TELDRIVE_ROOT}/rclone-crypt-recovery.txt"
readonly COMPOSE_FILE="${TELDRIVE_ROOT}/compose.yaml"
readonly ENV_FILE="${TELDRIVE_ROOT}/.env"
readonly RCLONE_BIN="${HOME}/.local/bin/rclone-teldrive"
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

die() {
	printf 'error: %s\n' "$*" >&2
	exit 1
}

command -v 7z >/dev/null || die "7z is required"
command -v podman >/dev/null || die "podman is required"
command -v python3 >/dev/null || die "python3 is required"
[[ -t 0 && -t 1 ]] || die "run this script in an interactive terminal"

[[ -d "$BACKUP_ROOT" ]] || die "backup destination is not mounted: $BACKUP_ROOT"
mountpoint -q /run/media/kylan/netac || die "the expected USB filesystem is not mounted"
[[ -f "$COMPOSE_FILE" ]] || die "missing TelDrive compose file: $COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || die "missing TelDrive environment file: $ENV_FILE"
[[ -f "$RCLONE_CONFIG" ]] || die "missing rclone config: $RCLONE_CONFIG"
[[ -f "$CRYPT_RECOVERY" ]] || die "missing rclone crypt recovery file: $CRYPT_RECOVERY"
[[ -x "$RCLONE_BIN" ]] || die "missing custom rclone binary: $RCLONE_BIN"
[[ -f "$SCRIPT_DIR/restore-teldrive.sh" ]] || die "missing companion restore-teldrive.sh"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
archive="${BACKUP_ROOT}/teldrive-recovery-${timestamp}.7z"
staging=$(mktemp -d "${TELDRIVE_ROOT}/backup-staging.XXXXXX")
chmod 700 "$staging"
cleanup() {
	rm -rf -- "$staging"
}
trap cleanup EXIT

mkdir -p "$staging/config" "$staging/database" "$staging/bin" "$staging/systemd"
cp -p "$COMPOSE_FILE" "$staging/config/compose.yaml"
cp -p "$ENV_FILE" "$staging/config/.env"
cp -p "$RCLONE_CONFIG" "$staging/config/rclone.conf"
cp -p "$CRYPT_RECOVERY" "$staging/config/rclone-crypt-recovery.txt"
cp -p "$RCLONE_BIN" "$staging/bin/rclone-teldrive"
cp -p "$SCRIPT_DIR/restore-teldrive.sh" "$staging/restore.sh"
cp -p "${HOME}/.config/systemd/user/teldrive-server.service" "$staging/systemd/"
cp -p "${HOME}/.config/systemd/user/teldrive-mount.service" "$staging/systemd/"
podman inspect teldrive-postgres-1 teldrive-teldrive-1 \
	--format '{{.Name}} {{.ImageName}} {{.Image}}' > "$staging/CONTAINER-IMAGES.txt"
"$RCLONE_BIN" version > "$staging/RCLONE-VERSION.txt"
# Pin the images actually running, rather than resolving moving tags at restore time.
postgres_image=$(podman inspect teldrive-postgres-1 --format '{{.Image}}')
server_image=$(podman inspect teldrive-teldrive-1 --format '{{.Image}}')
postgres_digest=$(podman image inspect "$postgres_image" --format '{{index .RepoDigests 0}}')
server_digest=$(podman image inspect "$server_image" --format '{{index .RepoDigests 0}}')
python3 - "$staging/config/compose.yaml" "$postgres_digest" "$server_digest" <<'PY'
import pathlib
import re
import sys
path = pathlib.Path(sys.argv[1])
images = iter(sys.argv[2:])
text, count = re.subn(r'(?m)^    image: .+$', lambda m: '    image: ' + next(images), path.read_text())
if count != 2 or not all('@sha256:' in image for image in sys.argv[2:]):
    raise SystemExit('Cannot pin the expected PostgreSQL and TelDrive images')
path.write_text(text)
PY

podman compose -f "$COMPOSE_FILE" exec -T postgres \
	pg_dump -U teldrive -d teldrive --no-owner --no-privileges \
	> "$staging/database/teldrive.sql"

cat > "$staging/START-HERE.txt" <<'EOF'
RESTORE ON A FRESH CACHYOS/ARCH LINUX X86-64 COMPUTER

1. Install 7-Zip if needed and extract this archive using its password.
2. Open a terminal in the extracted directory and run:

   bash restore.sh

3. Approve missing packages if asked. The script restores the database, keys,
   server, and automatic encrypted mount, then opens Dolphin.
4. Open a known file to verify recovery.

Run as your normal desktop user, NOT sudo. Internet is required.
Existing installations are refused rather than overwritten.
Keep your Telegram login/2FA available in case reauthentication is necessary.
See RESTORE.md if automatic recovery fails. Keep the extracted keys private.
This archive recovers access to Telegram files; it is not a copy of their contents.
EOF

cat > "$staging/RESTORE.md" <<'EOF'
# TelDrive recovery

This archive contains the TelDrive deployment configuration, PostgreSQL dump,
rclone configuration, and rclone crypt recovery material.

## Restore outline

1. Install Podman/Compose, FUSE 3, and 7-Zip on the replacement Linux computer.
2. Extract this archive only into a protected directory.
   Run `sha256sum -c SHA256SUMS` from the extracted directory.
   The included rclone binary is Linux amd64; rebuild this fork for other platforms.
3. Restore `config/compose.yaml` and `config/.env` under
   `~/.local/share/teldrive/`.
4. Restore `config/rclone.conf` under `~/.config/rclone/rclone.conf`.
   This includes all existing rclone remotes, not only TelDrive. Keep mode 600.
   Install `bin/rclone-teldrive` at `~/.local/bin/rclone-teldrive` (mode 755).
5. Start ONLY PostgreSQL with the restored compose file and a NEW empty volume:

   ```bash
   systemctl --user enable --now podman.socket
   podman compose -f ~/.local/share/teldrive/compose.yaml up -d postgres
   podman compose -f ~/.local/share/teldrive/compose.yaml exec -T postgres \
     pg_isready -U teldrive -d teldrive
   ```

   Wait for readiness before importing. Use CONTAINER-IMAGES.txt to identify
   the original versions; do not blindly upgrade the database or server during recovery.
6. Import `database/teldrive.sql` into the PostgreSQL container:

   ```bash
   podman compose -f ~/.local/share/teldrive/compose.yaml exec -T postgres \
     psql -v ON_ERROR_STOP=1 -U teldrive -d teldrive < database/teldrive.sql
   ```

7. Start TelDrive with `podman compose -f ~/.local/share/teldrive/compose.yaml up -d`
   and open `http://127.0.0.1:8080`. Telegram may require a new interactive login.
8. Recreate the rclone API key in TelDrive if the old key is unavailable.
9. Verify the encrypted remote:

   ```bash
   rclone-teldrive lsd teldrive-crypt:
   ```

10. After verifying a file can be read, restore `systemd/*.service` under
    `~/.config/systemd/user/`, create `~/Mounts/teldrive-crypt`, and run:

    ```bash
    systemctl --user daemon-reload
    systemctl --user enable --now teldrive-server.service teldrive-mount.service
    ```

This archive contains metadata and keys, not a second copy of the Telegram
file payloads. Recovery still depends on the Telegram account and channel.
An archive integrity test is not a database restore or end-to-end recovery test.

Keep `config/rclone-crypt-recovery.txt` and this archive protected. The rclone
crypt password and salt are required to decrypt remote names and file content.
Keep the TelDrive `security.data-key` and signing key from `.env` paired with
the database dump; changing them can make stored session data unreadable.
EOF

(cd "$staging" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)

printf 'Creating %s\n' "$archive"
printf 'Enter a strong non-empty password directly into 7-Zip and keep it separately.\n'
[[ ! -e "$archive" ]] || die "archive already exists: $archive"
(cd "$staging" && 7z a -t7z -mx=5 -mhe=on -p "$archive" .)
chmod 600 "$archive"

printf 'Testing archive integrity\n'
printf 'Re-enter the archive password when prompted to verify it.\n'
7z t "$archive"

printf '\nCreated and verified:\n%s\n' "$archive"
printf 'Size: '
du -h --apparent-size "$archive" | cut -f1
printf '\nKeep the archive password separately from the USB.\n'
