#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3 is required. On Ubuntu: sudo apt-get install python3' >&2
  exit 1
fi
exec python3 "$project_dir/scripts/terminal_ui.py" "$@"
