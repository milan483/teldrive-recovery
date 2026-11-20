#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Create an encrypted, independently restorable TelDrive recovery archive.
# Interactive mode uses 7z. --age-recipient enables unattended public-key encryption.

AGE_RECIPIENT=""
REQUIRED_MOUNT="/run/media/kylan/netac"
BACKUP_ROOT=""
while (( $# )); do
	case "$1" in
		--age-recipient)
			(( $# >= 2 )) || { printf 'error: --age-recipient needs a public key\n' >&2; exit 1; }
			AGE_RECIPIENT="$2"; shift 2 ;;
		--require-mount)
			(( $# >= 2 )) || { printf 'error: --require-mount needs a path\n' >&2; exit 1; }
			REQUIRED_MOUNT="$2"; shift 2 ;;
		--help)
			printf 'Usage: %s [--age-recipient age1...] [--require-mount /mounted/path] [destination]\n' "$0"; exit 0 ;;
		-*) printf 'error: unknown option: %s\n' "$1" >&2; exit 1 ;;
		*)
			[[ -z "$BACKUP_ROOT" ]] || { printf 'error: only one destination is allowed\n' >&2; exit 1; }
			BACKUP_ROOT="$1"; shift ;;
	esac
done
BACKUP_ROOT="${BACKUP_ROOT:-/run/media/kylan/netac/Obsidian USB}"
readonly AGE_RECIPIENT REQUIRED_MOUNT BACKUP_ROOT
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

command -v podman >/dev/null || die "podman is required"
command -v python3 >/dev/null || die "python3 is required"
if [[ -n "$AGE_RECIPIENT" ]]; then
	[[ "$AGE_RECIPIENT" =~ ^age1[023456789acdefghjklmnpqrstuvwxyz]{58}$ ]] || die "--age-recipient must be a valid-looking native age public key"
	command -v age >/dev/null || die "age is required for unattended encrypted recovery kits"
	command -v tar >/dev/null || die "tar is required"
else
	command -v 7z >/dev/null || die "7z is required"
	[[ -t 0 && -t 1 ]] || die "run this script in an interactive terminal or configure --age-recipient"
fi

[[ -d "$BACKUP_ROOT" ]] || die "backup destination is not mounted: $BACKUP_ROOT"
[[ -d "$REQUIRED_MOUNT" ]] || die "required mount is missing: $REQUIRED_MOUNT"
mountpoint -q -- "$REQUIRED_MOUNT" || die "the required filesystem is not mounted: $REQUIRED_MOUNT"
resolved_destination=$(realpath -e -- "$BACKUP_ROOT")
resolved_mount=$(realpath -e -- "$REQUIRED_MOUNT")
[[ "$resolved_mount" != / ]] || die "refusing to use the root filesystem as the required mount"
[[ "$resolved_destination" == "$resolved_mount" || "$resolved_destination" == "$resolved_mount/"* ]] || die "destination must be inside the required mounted filesystem"
[[ -f "$COMPOSE_FILE" ]] || die "missing TelDrive compose file: $COMPOSE_FILE"
[[ -f "$ENV_FILE" ]] || die "missing TelDrive environment file: $ENV_FILE"
[[ -f "$RCLONE_CONFIG" ]] || die "missing rclone config: $RCLONE_CONFIG"
[[ -f "$CRYPT_RECOVERY" ]] || die "missing rclone crypt recovery file: $CRYPT_RECOVERY"
[[ -x "$RCLONE_BIN" ]] || die "missing custom rclone binary: $RCLONE_BIN"
[[ -f "$SCRIPT_DIR/restore-teldrive.sh" ]] || die "missing companion restore-teldrive.sh"

timestamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -n "$AGE_RECIPIENT" ]]; then
	archive="${BACKUP_ROOT}/teldrive-recovery-${timestamp}.tar.age"
else
	archive="${BACKUP_ROOT}/teldrive-recovery-${timestamp}.7z"
fi
staging=$(mktemp -d "${TELDRIVE_ROOT}/backup-staging.XXXXXX")
chmod 700 "$staging"
partial=""
cleanup() {
	if [[ -n "$partial" ]]; then rm -f -- "$partial"; fi
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

1. Extract this archive to a private directory:
   - For .7z: use 7-Zip and the archive password.
   - For .tar.age: decrypt with your OFFLINE age private identity:
     age -d -i identity.txt -o recovery.tar teldrive-recovery-*.tar.age
     tar -xf recovery.tar
     Securely delete the temporary recovery.tar when finished.
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

1. Install Podman/Compose, FUSE 3, and 7-Zip or age on the replacement computer.
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
[[ ! -e "$archive" ]] || die "archive already exists: $archive"
if [[ -n "$AGE_RECIPIENT" ]]; then
	# Staging is private. Only ciphertext leaves this directory. Publish it after
	# tar and age both succeed, never leave a partial archive with a final name.
	partial=$(mktemp "${BACKUP_ROOT}/.teldrive-recovery-XXXXXXXX.tar.age")
	(cd "$staging" && tar -cf - .) | age -r "$AGE_RECIPIENT" > "$partial"
	[[ -s "$partial" ]] || die "age produced an empty encrypted archive"
	chmod 600 "$partial"
	mv -n -- "$partial" "$archive"
	[[ ! -e "$partial" ]] || die "archive appeared while encrypting; refusing to overwrite"
	partial=""
	printf 'Encrypted with the configured public key. Full integrity requires an offline decryption test.\n'
else
	printf 'Enter a strong non-empty password directly into 7-Zip and keep it separately.\n'
	(cd "$staging" && 7z a -t7z -mx=5 -mhe=on -p "$archive" .)
	chmod 600 "$archive"
	printf 'Testing archive integrity; re-enter the password when prompted.\n'
	7z t "$archive"
fi

printf '\nCreated and verified:\n%s\n' "$archive"
printf 'Size: '
du -h --apparent-size "$archive" | cut -f1
printf '\nKeep the password or age private identity separately from the archive.\n'
