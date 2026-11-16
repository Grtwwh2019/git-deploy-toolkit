#!/usr/bin/env python3
"""Create a change-only Linux deployment ZIP from two Git commits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tree_files(commit):
    # Git tree entries: MODE TYPE OBJECT<TAB>PATH<NUL>
    out = {}
    for record in git('ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if not record:
            continue
        meta, name = record.split(b'\t', 1)
        mode, kind, oid = meta.decode('ascii').split(' ')
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise ValueError(f'Unsupported Git entry: {os.fsdecode(name)} ({mode}, {kind})')
        out[name] = {'mode': mode, 'oid': oid}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base', help='Base commit SHA before push')
    ap.add_argument('target', help='Target commit SHA after push')
    ap.add_argument('--output-dir', default='packages')
    args = ap.parse_args()
    base = git('rev-parse', '--verify', f'{args.base}^{{commit}}').decode().strip()
    target = git('rev-parse', '--verify', f'{args.target}^{{commit}}').decode().strip()
    if base == target:
        raise SystemExit('No commit change; nothing to package.')
    if subprocess.run(['git','merge-base','--is-ancestor',base,target]).returncode != 0:
        raise SystemExit('Base is not an ancestor of target; refusing non-fast-forward deployment.')
    old, new = tree_files(base), tree_files(target)
    changes, payload = [], {}
    for raw_path in sorted(set(old) | set(new)):
        path = os.fsdecode(raw_path)
        # Reject ambiguous/nonportable paths and metadata collisions.
        parts = path.split('/')
        if (not path or path.startswith('/') or '\\' in path or ':' in path
                or any(p in ('', '.', '..') for p in parts)
                or any(p in ('.git', '.deployment-state') for p in parts)):
            raise SystemExit(f'Unsafe path: {path!r}')
        before, after = old.get(raw_path), new.get(raw_path)
        if before == after:
            continue
        status = 'A' if before is None else 'D' if after is None else 'M'
        entry = {'status': status, 'path': path, 'old_sha256': None, 'new_sha256': None,
                 'old_mode': before['mode'] if before else None,
                 'new_mode': after['mode'] if after else None}
        if before:
            entry['old_sha256'] = sha256(git('cat-file','blob',before['oid']))
        if after:
            data = git('cat-file','blob',after['oid'])
            entry['new_sha256'] = sha256(data)
            payload[path] = data
        changes.append(entry)
    if not changes:
        raise SystemExit('No file changes between these commits.')
    manifest = {'schema': 1, 'base_commit': base, 'target_commit': target,
                'changes': changes}
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / f'deployment_{base[:8]}_to_{target[:8]}.zip'
    root = Path(__file__).resolve().parent
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for path, data in payload.items():
            z.writestr('files/' + path, data)
        z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        z.writestr('deleted_files.txt', '\n'.join(c['path'] for c in changes if c['status']=='D') + '\n')
        for script in ('deploy.sh','rollback.sh','deploy_engine.py'):
            z.write(root / script, script)
    print(f'Created: {output.resolve()}')
    print(f'Changes: {len(changes)}, payload files: {len(payload)}')


if __name__ == '__main__':
    try:
        main()
    except (subprocess.CalledProcessError, ValueError, OSError) as exc:
        sys.exit(f'Packaging failed: {exc}')
