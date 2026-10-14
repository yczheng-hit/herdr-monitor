#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -x "$ROOT/.venv/bin/herdr-monitor" ]]; then
    printf 'Monitor environment missing. See %s/README.md for installation.\n' "$ROOT" >&2
    exit 1
fi
exec "$ROOT/.venv/bin/herdr-monitor" "$@"
