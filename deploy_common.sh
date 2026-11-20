#!/usr/bin/env bash
# Trusted program code shared by deploy.sh and rollback.sh. Metadata is never sourced.
source "$HERE/check-env.sh"
export LC_ALL=C

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

valid_relative() {
    local rel=$1
    [[ -n $rel && $rel != /* && $rel != *\\* && $rel != *:* && ! $rel =~ [[:cntrl:]] ]] || return 1
    case "/$rel/" in
        *//*|*/./*|*/../*|*/.git/*|*/.deployment-state/*) return 1 ;;
    esac
}

safe_path() {
    local current=$1 rel=$2 part
    local -a parts
    valid_relative "$rel" || fail "Invalid relative path: $rel"
    [[ ! -L $current ]] || fail "Symlink directory: $current"
    IFS=/ read -r -a parts <<< "$rel"
    for part in "${parts[@]}"; do
        current="$current/$part"
        [[ ! -L $current ]] || fail "Symlink in path: $current"
    done
    printf '%s\n' "$current"
}

load_manifest() {
    local line signature schema extra op oldhash newhash oldmode newmode rel j
    [[ -f $HERE/manifest.tsv && ! -L $HERE/manifest.tsv ]] || fail 'Missing/invalid manifest.tsv'
    COUNT=0
    {
        IFS=$'\t' read -r signature schema BASE DEST extra || fail 'Missing manifest header'
        [[ $signature == GIT_DEPLOY_TOOLKIT && $schema == 2 && -z $extra ]] || fail 'Unsupported manifest'
        [[ $BASE =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ && $DEST =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ && $BASE != "$DEST" ]] || fail 'Invalid commit IDs'
        while IFS= read -r line || [[ -n $line ]]; do
            IFS=$'\t' read -r op oldhash newhash oldmode newmode rel <<< "$line"
            valid_relative "$rel" || fail "Invalid manifest path: $rel"
            case $op in
                A) [[ $oldhash == - && $oldmode == - && $newhash =~ ^[0-9a-f]{64}$ && $newmode =~ ^100(644|755)$ ]] || fail 'Invalid added-file record' ;;
                D) [[ $newhash == - && $newmode == - && $oldhash =~ ^[0-9a-f]{64}$ && $oldmode =~ ^100(644|755)$ ]] || fail 'Invalid deleted-file record' ;;
                M) [[ $oldhash =~ ^[0-9a-f]{64}$ && $newhash =~ ^[0-9a-f]{64}$ && $oldmode =~ ^100(644|755)$ && $newmode =~ ^100(644|755)$ ]] || fail 'Invalid modified-file record' ;;
                *) fail 'Unknown change type' ;;
            esac
            for ((j=0; j<COUNT; j++)); do
                [[ $rel != "${PATHS[j]}" && $rel != "${PATHS[j]}/"* && ${PATHS[j]} != "$rel/"* ]] || fail 'Duplicate or file/directory transition in manifest'
            done
            OPS[COUNT]=$op; PATHS[COUNT]=$rel; OLD_HASH[COUNT]=$oldhash; NEW_HASH[COUNT]=$newhash
            OLD_MODE[COUNT]=$oldmode; NEW_MODE[COUNT]=$newmode
            COUNT=$((COUNT+1))
        done
    } < "$HERE/manifest.tsv"
    (( COUNT > 0 )) || fail 'Empty deployment package'
}

read_state() {
    [[ ! -L $STATE && ! -L $STATE/current_commit.txt ]] || fail 'Symlink in deployment state'
    [[ ! -e $STATE || -d $STATE ]] || fail 'Deployment state is not a directory'
    CURRENT=''
    if [[ -e $STATE/current_commit.txt ]]; then
        [[ -f $STATE/current_commit.txt ]] || fail 'Deployment version is not a regular file'
        CURRENT=$(< "$STATE/current_commit.txt")
        [[ $CURRENT =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || fail 'Invalid deployment version file'
    fi
}

cleanup() {
    local code=$?
    trap - EXIT
    if [[ -n ${TEMP_FILE:-} && -f $TEMP_FILE ]]; then rm -f -- "$TEMP_FILE" || :; fi
    if [[ ${LOCKED:-0} == 1 ]]; then rmdir -- "$STATE/lock" || :; fi
    if (( code != 0 )) && [[ ${STARTED:-0} == 1 ]]; then
        printf 'Operation interrupted. Keep this release and backup; run rollback.sh --dry-run before recovery.\n' >&2
    fi
    exit "$code"
}

lock_target() {
    mkdir -p -- "$STATE"
    mkdir -- "$STATE/lock" 2>/dev/null || fail "Another operation or stale lock exists: $STATE/lock"
    LOCKED=1
}

atomic_copy() {
    local src=$1 dest=$2 mode=$3 parent=${2%/*}
    mkdir -p -- "$parent"
    TEMP_FILE=$(mktemp "$parent/.git-deploy.XXXXXX")
    cp -- "$src" "$TEMP_FILE"
    chmod "$mode" "$TEMP_FILE"
    mv -f -- "$TEMP_FILE" "$dest"
    TEMP_FILE=''
}

atomic_text() {
    local dest=$1 value=$2 parent=${1%/*}
    TEMP_FILE=$(mktemp "$parent/.git-deploy.XXXXXX")
    printf '%s\n' "$value" > "$TEMP_FILE"
    mv -f -- "$TEMP_FILE" "$dest"
    TEMP_FILE=''
}

preflight_deploy() {
    local i path src parent
    read_state
    [[ -z $CURRENT || $CURRENT == "$BASE" ]] || fail "Deployed version $CURRENT does not match base $BASE"
    [[ ! -e $BACKUP && ! -L $BACKUP ]] || fail 'Backup already exists; use a fresh release extraction'
    [[ ! -e $STATE/lock && ! -L $STATE/lock || $LOCKED == 1 ]] || fail 'Another operation or stale lock exists'
    for ((i=0; i<COUNT; i++)); do
        path=$(safe_path "$TARGET" "${PATHS[i]}")
        [[ ! -e $path || -f $path ]] || fail "Target is not a regular file: $path"
        parent=${path%/*}
        while [[ $parent != "$TARGET" ]]; do
            [[ ! -e $parent || -d $parent ]] || fail "Non-directory parent: $parent"
            parent=${parent%/*}
        done
        if [[ ${OLD_HASH[i]} == - ]]; then
            [[ ! -e $path ]] || fail "New file would overwrite local data: $path"
        else
            [[ -f $path && $(hash_file "$path") == "${OLD_HASH[i]}" ]] || fail "Old content differs: $path"
        fi
        if [[ ${NEW_HASH[i]} != - ]]; then
            src=$(safe_path "$HERE/files" "${PATHS[i]}")
            [[ -f $src && $(hash_file "$src") == "${NEW_HASH[i]}" ]] || fail "Payload checksum mismatch: $src"
        fi
    done
}

run_deploy() {
    local i path saved mode
    preflight_deploy
    printf 'Base: %s\nTarget: %s\nRoot: %s\n' "$BASE" "$DEST" "$TARGET"
    [[ -n $CURRENT ]] || printf 'First deployment: assuming the server matches base; all affected old files were checked.\n'
    for ((i=0; i<COUNT; i++)); do printf '%s %s\n' "${OPS[i]}" "${PATHS[i]}"; done
    if (( DRY )); then printf 'DRY RUN ONLY; no files, state or backups changed.\n'; return; fi
    lock_target
    preflight_deploy
    mkdir -- "$BACKUP"
    mkdir -- "$BACKUP/files" "$BACKUP/touched"
    printf '%s\n' "$TARGET" > "$BACKUP/target.path"
    hash_file "$HERE/manifest.tsv" > "$BACKUP/manifest.sha256"
    if [[ -f $STATE/current_commit.txt ]]; then cp -p -- "$STATE/current_commit.txt" "$BACKUP/state.before"; fi
    # Complete every backup before changing any application file.
    for ((i=0; i<COUNT; i++)); do
        if [[ ${OLD_HASH[i]} != - ]]; then
            path=$(safe_path "$TARGET" "${PATHS[i]}")
            saved=$(safe_path "$BACKUP/files" "${PATHS[i]}")
            mkdir -p -- "${saved%/*}"
            cp -p -- "$path" "$saved"
            [[ $(hash_file "$saved") == "${OLD_HASH[i]}" ]] || fail "Backup checksum mismatch: $saved"
        fi
    done
    atomic_text "$BACKUP/status" deploying
    STARTED=1
    for ((i=0; i<COUNT; i++)); do
        path=$(safe_path "$TARGET" "${PATHS[i]}")
        # Write intent first: rollback accepts either old or new bytes after an interruption.
        : > "$BACKUP/touched/$i"
        if [[ ${OPS[i]} == D ]]; then
            rm -- "$path"
        else
            mode=${NEW_MODE[i]#100}
            if [[ ${OPS[i]} == M && ${OLD_MODE[i]} == "${NEW_MODE[i]}" ]]; then
                mode=$(file_mode "$BACKUP/files/${PATHS[i]}")
            fi
            atomic_copy "$HERE/files/${PATHS[i]}" "$path" "$mode"
        fi
    done
    atomic_text "$STATE/current_commit.txt" "$DEST"
    atomic_text "$BACKUP/status" deployed
    STARTED=0
    printf 'DEPLOYMENT COMPLETE: %s\nBackup: %s\n' "$DEST" "$BACKUP"
}

preflight_rollback() {
    local i path saved actual expected_target fingerprint
    read_state
    [[ -d $BACKUP && ! -L $BACKUP && -d $BACKUP/touched && ! -L $BACKUP/touched ]] || fail 'No valid backup found'
    for path in target.path manifest.sha256 status; do
        [[ -f $BACKUP/$path && ! -L $BACKUP/$path ]] || fail "Invalid backup metadata: $path"
    done
    expected_target=$(< "$BACKUP/target.path")
    [[ $expected_target == "$TARGET" ]] || fail 'Backup belongs to a different deployment root'
    fingerprint=$(< "$BACKUP/manifest.sha256")
    [[ $(hash_file "$HERE/manifest.tsv") == "$fingerprint" ]] || fail 'Manifest differs from the backed-up release'
    STATUS=$(< "$BACKUP/status")
    [[ $STATUS == deploying || $STATUS == deployed || $STATUS == rolling_back ]] || fail "Cannot roll back backup status: $STATUS"
    [[ -z $CURRENT || $CURRENT == "$BASE" || $CURRENT == "$DEST" ]] || fail 'A different release is currently deployed'
    if [[ $STATUS == deployed ]]; then [[ $CURRENT == "$DEST" ]] || fail 'Deployment version changed after deployment'; fi
    if [[ -e $BACKUP/state.before || -L $BACKUP/state.before ]]; then
        [[ -f $BACKUP/state.before && ! -L $BACKUP/state.before && $(< "$BACKUP/state.before") == "$BASE" ]] || fail 'Invalid original deployment version'
    fi
    [[ ! -e $STATE/lock && ! -L $STATE/lock || $LOCKED == 1 ]] || fail 'Another operation or stale lock exists'
    for ((i=0; i<COUNT; i++)); do
        [[ ! -L $BACKUP/touched/$i ]] || fail 'Symlink in rollback journal'
        [[ -f $BACKUP/touched/$i ]] || continue
        path=$(safe_path "$TARGET" "${PATHS[i]}")
        [[ ! -e $path || -f $path ]] || fail "Non-file at rollback path: $path"
        actual=-
        if [[ -f $path ]]; then actual=$(hash_file "$path"); fi
        if [[ $STATUS == deployed ]]; then
            [[ $actual == "${NEW_HASH[i]}" ]] || fail "Deployed file changed: $path"
        else
            [[ $actual == "${NEW_HASH[i]}" || $actual == "${OLD_HASH[i]}" ]] || fail "Unexpected content during recovery: $path"
        fi
        if [[ ${OLD_HASH[i]} != - ]]; then
            saved=$(safe_path "$BACKUP/files" "${PATHS[i]}")
            [[ -f $saved && $(hash_file "$saved") == "${OLD_HASH[i]}" ]] || fail "Missing/corrupt backup: $saved"
        fi
    done
}

run_rollback() {
    local i path saved
    preflight_rollback
    for ((i=COUNT-1; i>=0; i--)); do
        [[ -f $BACKUP/touched/$i ]] || continue
        if [[ ${OLD_HASH[i]} == - ]]; then printf 'REMOVE %s\n' "${PATHS[i]}"; else printf 'RESTORE %s\n' "${PATHS[i]}"; fi
    done
    if (( DRY )); then printf 'DRY RUN ONLY; no files, state or backups changed.\n'; return; fi
    lock_target
    preflight_rollback
    atomic_text "$BACKUP/status" rolling_back
    STARTED=1
    for ((i=COUNT-1; i>=0; i--)); do
        [[ -f $BACKUP/touched/$i ]] || continue
        path=$(safe_path "$TARGET" "${PATHS[i]}")
        if [[ ${OLD_HASH[i]} == - ]]; then
            if [[ -e $path ]]; then rm -- "$path"; fi
        else
            saved=$(safe_path "$BACKUP/files" "${PATHS[i]}")
            atomic_copy "$saved" "$path" "$(file_mode "$saved")"
        fi
        rm -- "$BACKUP/touched/$i"
    done
    if [[ -f $BACKUP/state.before ]]; then
        atomic_copy "$BACKUP/state.before" "$STATE/current_commit.txt" 644
    elif [[ -e $STATE/current_commit.txt ]]; then
        rm -- "$STATE/current_commit.txt"
    fi
    atomic_text "$BACKUP/status" rolled_back
    STARTED=0
    printf 'ROLLBACK COMPLETE: %s\n' "$BASE"
}

main() {
    local action=$1 root
    shift
    [[ $# == 1 || $# == 2 && $2 == --dry-run ]] || fail "Usage: bash $action.sh /absolute/target/root [--dry-run]"
    root=$1
    [[ $root == /* && $root != / && ! $root =~ [[:cntrl:]] ]] || fail 'Use an absolute deployment directory other than /'
    DRY=0; [[ $# == 1 ]] || DRY=1
    require_environment || fail 'Required commands are unavailable; no deployment files were changed'
    # Check root components before canonicalizing, so symlink traversal cannot be hidden.
    root=${root%/}
    safe_path / "${root#/}" >/dev/null
    TARGET=$(cd -P -- "$root" && pwd -P) || fail 'Target directory does not exist'
    [[ $TARGET != / ]] || fail 'Refusing filesystem root'
    case "$HERE/" in "$TARGET/"*) fail 'Release directory must be outside the deployment root' ;; esac
    case "$TARGET/" in "$HERE/"*) fail 'Deployment root must be outside the release directory' ;; esac
    STATE="$TARGET/.deployment-state"; BACKUP="$HERE/backup"
    LOCKED=0; STARTED=0; TEMP_FILE=''
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    load_manifest
    case $action in deploy) run_deploy ;; rollback) run_rollback ;; *) fail 'Unknown action' ;; esac
}
