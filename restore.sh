#!/usr/bin/env bash
# Stable short entrypoint for the recovery script.
set -Eeuo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$script_dir/restore-teldrive.sh" "$@"
