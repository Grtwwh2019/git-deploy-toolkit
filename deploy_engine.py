#!/usr/bin/env python3
"""Conservative incremental deployment: checks hashes, backups, version, rollback.
Requires Python >=3.9 on Linux. No symlink traversal. One deployment at a time.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


def fail(msg):
    raise RuntimeError(msg)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe(root, rel):
    if (not isinstance(rel, str) or not rel or rel.startswith('/') or '\\' in rel
            or ':' in rel or any(x in ('', '.', '..') for x in rel.split('/'))):
        fail(f'Invalid path: {rel!r}')
    p = root
    for part in rel.split('/'):
        p = p / part
        if p.is_symlink():
            fail(f'Symlink in path: {p}')
    return p


def write_atomic(source, destination, mode):
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.deploy-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as f, source.open('rb') as s:
            shutil.copyfileobj(s, f)
        os.chmod(tmp, 0o755 if mode == '100755' else 0o644)
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run():
    if len(sys.argv) not in (4, 5):
        fail('Usage: deploy_engine.py deploy|rollback PACKAGE_DIR TARGET_DIR [--dry-run]')
    action, pkgarg, targetarg = sys.argv[1:4]
    dry = len(sys.argv) == 5 and sys.argv[4] == '--dry-run'
    if len(sys.argv) == 5 and not dry:
        fail('Unknown option')
    package = Path(pkgarg).resolve(strict=True)
    target = Path(targetarg).resolve(strict=True)
    if not target.is_dir() or target == Path('/'):
        fail('Target must be an existing directory other than /')
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('schema') != 1:
        fail('Unsupported manifest')
    base, dest = manifest['base_commit'], manifest['target_commit']
    state_dir = target / '.deployment-state'
    if state_dir.is_symlink():
        fail('Deployment state directory cannot be a symlink')
    state_file = state_dir / 'current_commit.txt'
    backup = package / 'backup'
    journal = backup / 'journal.json'
    changes = manifest['changes']
    paths = [c['path'] for c in changes]
    if len(paths) != len(set(paths)):
        fail('Duplicate paths in manifest')
    if any(p.split('/')[0] == '.deployment-state' for p in paths):
        fail('Deployment state path is reserved')
    current = state_file.read_text().strip() if state_file.exists() else None
    if action == 'deploy':
        if current != base:
            fail(f'Current deployed commit {current!r} does not equal package base {base!r}. '
                 'Initialize state only after independently verifying Linux matches that commit.')
        if journal.exists():
            fail('Existing backup journal: rollback or archive it before deploying again')
        ops = []
        for c in changes:
            path = safe(target, c['path'])
            if path.exists() and not path.is_file():
                fail(f'Target is not a regular file: {path}')
            old = c['old_sha256']
            if old is None and path.exists():
                fail(f'New file would overwrite local data: {path}')
            if old is not None and (not path.is_file() or digest(path) != old):
                fail(f'Existing file does not match expected old content: {path}')
            src = None
            if c['new_sha256'] is not None:
                src = safe(package / 'files', c['path'])
                if not src.is_file() or digest(src) != c['new_sha256']:
                    fail(f'Payload checksum mismatch: {src}')
            ops.append((c, path, src))
        for c, _, _ in ops:
            print(f"{c['status']} {c['path']}")
        if dry:
            print('DRY RUN ONLY; no changes made')
            return
        if backup.exists():
            fail(f'Backup directory already exists: {backup}')
        backup.mkdir(parents=True)
        record = {'base_commit': base, 'target_commit': dest, 'completed': False,
                  'applied': [], 'changes': changes}
        def save_journal():
            tmp = backup / 'journal.tmp'
            tmp.write_text(json.dumps(record, indent=2), encoding='utf-8')
            os.replace(tmp, journal)
        save_journal()
        try:
            for c, path, src in ops:
                if path.exists():
                    b = safe(backup / 'files', c['path'])
                    b.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, b)
                record['applied'].append(c['path'])
                save_journal()
                if src is None:
                    path.unlink()
                else:
                    write_atomic(src, path, c['new_mode'])
            state_dir.mkdir(parents=True, exist_ok=True)
            state_file.write_text(dest + '\n', encoding='utf-8')
            record['completed'] = True
            save_journal()
            print('DEPLOYMENT COMPLETE:', dest)
        except Exception:
            print('Deployment interrupted. Run rollback.sh before retrying.', file=sys.stderr)
            raise
    elif action == 'rollback':
        if not journal.exists():
            fail('No backup journal found')
        record = json.loads(journal.read_text(encoding='utf-8'))
        if record['base_commit'] != base or record['target_commit'] != dest:
            fail('Backup does not match this package')
        if current not in (base, dest):
            fail(f'Unexpected deployed version: {current}')
        by_path = {c['path']: c for c in changes}
        applied = record['applied']
        # Refuse rollback if deployed files were changed after deployment.
        for rel in applied:
            c = by_path[rel]
            path = safe(target, rel)
            if path.exists() and not path.is_file():
                fail(f'Non-file at rollback path: {path}')
            expected = c['new_sha256']
            if expected is None:
                if path.exists():
                    fail(f'Deleted path reappeared: {path}')
            elif path.is_file() and digest(path) != expected:
                fail(f'Deployed file has been modified: {path}')
        for rel in reversed(applied):
            c = by_path[rel]
            print(f'RESTORE {rel}' if c['old_sha256'] else f'REMOVE {rel}')
        if dry:
            print('DRY RUN ONLY; no changes made')
            return
        for rel in reversed(applied):
            c = by_path[rel]
            path = safe(target, rel)
            if c['old_sha256'] is None:
                if path.exists():
                    path.unlink()
            else:
                old = safe(backup / 'files', rel)
                if not old.is_file() or digest(old) != c['old_sha256']:
                    fail(f'Missing/corrupt backup: {old}')
                write_atomic(old, path, c['old_mode'])
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file.write_text(base + '\n', encoding='utf-8')
        # Keep backup for audit, but rename journal to allow next deployment.
        journal.rename(backup / 'rolled_back.json')
        print('ROLLBACK COMPLETE:', base)
    else:
        fail(f'Unknown action: {action}')


if __name__ == '__main__':
    try:
        run()
    except (RuntimeError, OSError, KeyError, ValueError) as exc:
        sys.exit(f'ERROR: {exc}')
