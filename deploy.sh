#!/usr/bin/env bash
set -euo pipefail
HERE=${BASH_SOURCE[0]%/*}
[[ $HERE != "${BASH_SOURCE[0]}" ]] || HERE=.
HERE=$(cd -P -- "$HERE" && pwd -P)
source "$HERE/deploy_common.sh"
main deploy "$@"
