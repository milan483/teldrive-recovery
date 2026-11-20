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

usage() {
	cat <<'EOF'
Usage:
  bash restore.sh [--check]
  bash restore.sh --cleanup

--check    Validate the extracted kit and destination without changing anything.
--cleanup  Remove only a confirmed partial restore from this kit.
EOF
}

[[ ${1:-} == '' || ${1:-} == --check || ${1:-} == --cleanup ]] || { usage >&2; exit 2; }
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || die 'this kit supports Linux x86-64 only'
[[ $EUID != 0 ]] || die 'run as your normal desktop user, not with sudo'
command -v pacman >/dev/null || die 'automatic restore currently supports Arch/CachyOS only'
for file in config/compose.yaml config/.env config/rclone.conf \
	config/rclone-crypt-recovery.txt database/teldrive.sql bin/rclone-teldrive \
	systemd/teldrive-server.service systemd/teldrive-mount.service SHA256SUMS; do
	[[ -s "$kit/$file" && ! -L "$kit/$file" ]] || die "missing or empty kit file: $file"
done
(cd "$kit" && sha256sum --check --quiet SHA256SUMS)

cleanup_partial() {
	local volumes containers
	volumes=$(podman volume ls --format '{{.Name}}' | grep -E '(^|[-_])teldrive([-_].*)?$' || true)
	containers=$(podman ps -aq \
		--filter 'label=io.podman.compose.project=teldrive' \
		--filter 'name=teldrive' || true)
	if [[ -z "$volumes$containers" && ! -e "$root" && ! -e "$config" ]]; then
		printf 'No partial TelDrive restore was found.\n'
		return 0
	fi
	printf 'This removes only the following detected partial state:\n'
	[[ -n "$containers" ]] && printf 'containers:\n%s\n' "$containers"
	[[ -n "$volumes" ]] && printf 'volumes:\n%s\n' "$volumes"
	[[ -e "$root" ]] && printf 'directory: %s\n' "$root"
	[[ -e "$config" ]] && printf 'file: %s\n' "$config"
	printf 'Continue? This is destructive and must only be used for a failed fresh restore. [y/N] '
	read -r answer
	[[ $answer == y || $answer == Y ]] || die 'cancelled; nothing was removed'
	systemctl --user disable --now teldrive-mount.service teldrive-server.service 2>/dev/null || true
	[[ -z "$containers" ]] || podman rm -f $containers >/dev/null
	[[ -z "$volumes" ]] || podman volume rm $volumes >/dev/null
	rm -rf -- "$root"
	rm -f -- "$config" "$HOME/.local/bin/rclone-teldrive" \
		"$units/teldrive-server.service" "$units/teldrive-mount.service"
	systemctl --user daemon-reload
	printf 'Partial restore removed. Run bash restore.sh to retry.\n'
}

if [[ ${1:-} == --cleanup ]]; then
	[[ -t 0 ]] || die '--cleanup requires an interactive terminal'
	cleanup_partial
	exit 0
fi

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
existing_volumes=$(podman volume ls --format '{{.Name}}' | grep -E '(^|[-_])teldrive([-_].*)?$' || true)
existing_containers=$(podman ps -aq --filter 'label=io.podman.compose.project=teldrive' --filter 'name=teldrive' || true)
[[ -z "$existing_volumes" ]] || die "existing TelDrive volume found; if this is a failed fresh restore run: bash restore.sh --cleanup"
[[ -z "$existing_containers" ]] || die "existing TelDrive container found; if this is a failed fresh restore run run: bash restore.sh --cleanup"
if curl -s --connect-timeout 2 http://127.0.0.1:8080/ >/dev/null; then
	die 'port 8080 already serves another application'
fi
mkdir -p "$root" "$(dirname "$config")" "$HOME/.local/bin" "$units" "$HOME/Mounts/teldrive-crypt"
install -m 600 "$kit/config/compose.yaml" "$root/compose.yaml"
install -m 600 "$kit/config/.env" "$root/.env"
install -m 600 "$kit/config/rclone.conf" "$config"
install -m 600 "$kit/config/rclone-crypt-recovery.txt" "$root/"
install -m 755 "$kit/bin/rclone-teldrive" "$HOME/.local/bin/"
# Strict rootless Podman rejects unqualified image names; normalize old kits too.
sed -i \
	-e 's#image: postgres:18-alpine#image: docker.io/library/postgres:18-alpine#' \
	-e 's#image: postgres:17-alpine#image: docker.io/library/postgres:17-alpine#' \
	-e 's#image: postgres:16-alpine#image: docker.io/library/postgres:16-alpine#' \
	"$root/compose.yaml"
compose() { podman compose -p teldrive -f "$root/compose.yaml" "$@"; }
service_exec() {
	local service=$1
	shift
	local container
	container=$(podman ps -q \
		--filter "label=io.podman.compose.project=teldrive" \
		--filter "label=io.podman.compose.service=${service}" | head -n1)
	[[ -n "$container" ]] || die "container for service is not running: $service"
	podman exec -i "$container" "$@"
}
compose up -d postgres
ready=false
for ((attempt=0; attempt<60; attempt++)); do
	if service_exec postgres pg_isready -U teldrive -d teldrive >/dev/null 2>&1; then
		ready=true
		break
	fi
	sleep 2
done
$ready || die 'PostgreSQL did not become ready; inspect container logs'
service_exec postgres psql -v ON_ERROR_STOP=1 -U teldrive -d teldrive < "$kit/database/teldrive.sql"
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
