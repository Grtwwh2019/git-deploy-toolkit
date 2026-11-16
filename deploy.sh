#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 ]]; then echo 'Usage: bash deploy.sh /absolute/target/path [--dry-run]' >&2; exit 2; fi
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 "$HERE/deploy_engine.py" deploy "$HERE" "$@"
