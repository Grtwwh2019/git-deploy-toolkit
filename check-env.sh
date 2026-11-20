#!/usr/bin/env bash
# Can be run on its own before copying a release to the ETL server.
require_environment() {
    if [[ -z ${BASH_VERSION:-} ]] || (( BASH_VERSINFO[0] < 3 || (BASH_VERSINFO[0] == 3 && BASH_VERSINFO[1] < 2) )); then
        printf 'ERROR: Bash 3.2 or newer is required.\n' >&2
        return 1
    fi
    local cmd missing=0
    for cmd in cp mv rm mkdir rmdir chmod mktemp stat; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            printf 'MISSING: %s\n' "$cmd" >&2
            missing=1
        fi
    done
    SHA256_TOOL=''
    for cmd in sha256sum shasum openssl; do
        if command -v "$cmd" >/dev/null 2>&1; then
            SHA256_TOOL=$cmd
            break
        fi
    done
    if [[ -z $SHA256_TOOL ]]; then
        printf 'MISSING: one of sha256sum, shasum, openssl (SHA-256 verification).\n' >&2
        missing=1
    fi
    (( missing == 0 )) || return 1
    hash_file /dev/null >/dev/null || return 1
    file_mode /dev/null >/dev/null || return 1
    printf 'Environment OK: Bash %s, SHA-256 via %s. No Python or Git needed.\n' "$BASH_VERSION" "$SHA256_TOOL"
}

hash_file() {
    local result
    case "$SHA256_TOOL" in
        sha256sum) result=$(sha256sum < "$1") || return 1; result=${result%% *} ;;
        shasum) result=$(shasum -a 256 < "$1") || return 1; result=${result%% *} ;;
        openssl) result=$(openssl dgst -sha256 < "$1") || return 1; result=${result##* } ;;
        *) printf 'ERROR: No SHA-256 tool selected.\n' >&2; return 1 ;;
    esac
    [[ $result =~ ^[0-9a-f]{64}$ ]] || { printf 'ERROR: Invalid SHA-256 output.\n' >&2; return 1; }
    printf '%s\n' "$result"
}

file_mode() {
    local result
    if result=$(stat -c '%a' -- "$1" 2>/dev/null) && [[ $result =~ ^[0-7]{3,4}$ ]]; then
        printf '%s\n' "$result"
    elif result=$(stat -f '%Lp' -- "$1" 2>/dev/null) && [[ $result =~ ^[0-7]{3,4}$ ]]; then
        printf '%s\n' "$result"
    else
        printf 'ERROR: stat cannot read file permissions: %s\n' "$1" >&2
        return 1
    fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    set -euo pipefail
    export LC_ALL=C
    require_environment
fi
