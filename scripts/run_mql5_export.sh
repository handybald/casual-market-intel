#!/usr/bin/env bash
# Usage: ./scripts/run_mql5_export.sh 2025-09-01 2025-09-30 [extra run_mql5_export.py flags]
set -euo pipefail
if [ "$#" -lt 2 ]; then
  echo "usage: $0 START_YYYY-MM-DD END_YYYY-MM-DD [--force-compile ...]" >&2
  exit 2
fi
start="$1"; end="$2"; shift 2
here="$(cd "$(dirname "$0")" && pwd)"
py=python3
[ -x "$here/../.venv/bin/python" ] && py="$here/../.venv/bin/python"  # needs PyYAML from the repo venv
exec "$py" "$here/run_mql5_export.py" --start "$start" --end "$end" "$@"
