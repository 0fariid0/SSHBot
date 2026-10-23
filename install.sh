#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run: sudo bash install.sh'; exit 1; }
SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null || { echo 'Python 3 is required (Ubuntu 22.04/24.04).'; exit 1; }
exec python3 "$SOURCE/deploy/manager.py" --source "$SOURCE" "$@"
