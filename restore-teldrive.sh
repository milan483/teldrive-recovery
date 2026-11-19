#!/usr/bin/env bash
# Restore a recovery kit on a fresh Arch/CachyOS Linux x86-64 installation.
set -Eeuo pipefail
umask 077

die() { printf 'Restore stopped: %s\n' "$*" >&2; exit 1; }
kit=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root="$HOME/.local/share/teldrive"
config="$HOME/.config/rclone/rclone.conf"
units="$HOME/.config/systemd/user"
trap 'printf "Restore failed at line %s. Existing restored data is retained; do not rerun over it.\n" "$LINENO" >&2' ERR

[[ ${1:-} == '' || ${1:-} == --check ]] || die 'usage: bash restore.sh [--check]'
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'this kit supports Linux x86-64 only'
[[ $EUID != 0 ]] || die 'run as your normal desktop user, not with sudo'
command -v pacman >/dev/null || die 'automatic restore currently supports Arch/CachyOS only'
for file in config/compose.yaml config/.env config/rclone.conf \
	config/rclone-crypt-recovery.txt database/teldrive.sql bin/rclone-teldrive \
	systemd/teldrive-server.service systemd/teldrive-mount.service SHA256SUMS; do
	[[ -s "$kit/$file" && ! -L "$kit/$file" ]] || die "missing or empty kit file: $file"
done
(cd "$kit" && sha256sum --check --quiet SHA256SUMS)
for path in "$root" "$config" "$HOME/.local/bin/rclone-teldrive" \
	"$units/teldrive-server.service" "$units/teldrive-mount.service"; do
	[[ ! -e "$path" && ! -L "$path" ]] || die "refusing to overwrite existing path: $path"
done
if [[ ${1:-} == --check ]]; then
	printf 'Kit checksums and destination checks passed. No changes made.\n'
	exit 0
fi
[[ -t 0 ]] || die 'run in an interactive terminal'
printf 'Restore TelDrive, its keys, and automatic encrypted mount on this computer? [y/N] '
read -r answer
[[ $answer == y || $answer == Y ]] || die 'cancelled'
missing=()
for pair in podman:podman podman-compose:podman-compose fusermount3:fuse3 dolphin:dolphin curl:curl; do
	command -v "${pair%%:*}" >/dev/null || missing+=("${pair#*:}")
done
if ((${#missing[@]})); then
	printf 'Missing packages: %s\n' "${missing[*]}"
	printf 'Install with sudo pacman -Syu --needed? [y/N] '
	read -r answer
	[[ $answer == y || $answer == Y ]] || die 'packages were not installed'
	sudo pacman -Syu --needed "${missing[@]}"
fi
export PODMAN_COMPOSE_PROVIDER
PODMAN_COMPOSE_PROVIDER=$(command -v podman-compose)
systemctl --user enable --now podman.socket
# The kit uses the original project name and must not attach to an old volume.
[[ -z $(podman volume ls -q --filter name='^teldrive_postgres-data$') ]] || die 'existing TelDrive database volume found'
[[ -z $(podman ps -aq --filter name='^teldrive[-_]') ]] || die 'existing TelDrive containers found'
if curl -s --connect-timeout 2 http://127.0.0.1:8080/ >/dev/null; then
	die 'port 8080 already serves another application'
fi
mkdir -p "$root" "$(dirname "$config")" "$HOME/.local/bin" "$units" "$HOME/Mounts/teldrive-crypt"
install -m 600 "$kit/config/compose.yaml" "$root/compose.yaml"
install -m 600 "$kit/config/.env" "$root/.env"
install -m 600 "$kit/config/rclone.conf" "$config"
install -m 600 "$kit/config/rclone-crypt-recovery.txt" "$root/"
install -m 755 "$kit/bin/rclone-teldrive" "$HOME/.local/bin/"
compose() { podman compose -p teldrive -f "$root/compose.yaml" "$@"; }
compose up -d postgres
ready=false
for ((attempt=0; attempt<60; attempt++)); do
	if compose exec -T postgres pg_isready -U teldrive -d teldrive >/dev/null 2>&1; then
		ready=true
		break
	fi
	sleep 2
done
$ready || die 'PostgreSQL did not become ready; inspect container logs'
compose exec -T postgres psql -v ON_ERROR_STOP=1 -U teldrive -d teldrive < "$kit/database/teldrive.sql"
compose up -d
ready=false
for ((attempt=0; attempt<60; attempt++)); do
	if curl -fsS --max-time 3 http://127.0.0.1:8080/ >/dev/null 2>&1; then
		ready=true
		break
	fi
	sleep 2
done
$ready || die 'TelDrive did not become ready; inspect container logs'
"$HOME/.local/bin/rclone-teldrive" lsd teldrive-crypt: --contimeout 10s --timeout 30s --retries 1
install -m 644 "$kit/systemd/"*.service "$units/"
systemctl --user daemon-reload
systemctl --user enable --now teldrive-server.service teldrive-mount.service
ready=false
for ((attempt=0; attempt<30; attempt++)); do
	if mountpoint -q "$HOME/Mounts/teldrive-crypt"; then ready=true; break; fi
	sleep 1
done
$ready || die 'encrypted mount did not become ready'
dolphin "$HOME/Mounts/teldrive-crypt" >/dev/null 2>&1 &
printf '\nRestored and mounted: %s\n' "$HOME/Mounts/teldrive-crypt"
printf 'Open a known file to verify decryption. Remove extracted secrets when finished.\n'
