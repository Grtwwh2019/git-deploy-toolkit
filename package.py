#!/usr/bin/env python3
"""Build a ZIP from the net changes between two Git commits. Linux needs only Bash."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

RUNTIME = ('deploy.sh', 'rollback.sh', 'deploy_common.sh', 'check-env.sh')


def git(*args):
    return subprocess.check_output(['git', *args], stderr=subprocess.PIPE)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tree_files(commit):
    result = {}
    for record in git('ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
        if record:
            metadata, name = record.split(b'\t', 1)
            mode, kind, oid = metadata.decode('ascii').split(' ')
            result[name] = {'mode': mode, 'kind': kind, 'oid': oid}
    return result


def validate_path(raw_path):
    path = raw_path.decode('utf-8')
    parts = path.split('/')
    if (not path or path.startswith('/') or '\\' in path or ':' in path
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(p in ('', '.', '..', '.git', '.deployment-state') for p in parts)):
        raise ValueError(f'Unsafe/nonportable path: {path!r}')
    return path


def plan(base, target):
    old, new = tree_files(base), tree_files(target)
    changes, payload = [], {}
    for raw_path in sorted(set(old) | set(new)):
        before, after = old.get(raw_path), new.get(raw_path)
        if before == after:
            continue
        path = validate_path(raw_path)
        for entry in (before, after):
            if entry and (entry['kind'] != 'blob' or entry['mode'] not in ('100644', '100755')):
                raise ValueError(f'Changed symlink/submodule/special entry is unsupported: {path}')
        entry = {
            'status': 'A' if before is None else 'D' if after is None else 'M',
            'path': path, 'old_sha256': None, 'new_sha256': None,
            'old_mode': before['mode'] if before else None,
            'new_mode': after['mode'] if after else None,
        }
        if before:
            entry['old_sha256'] = sha256(git('cat-file', 'blob', before['oid']))
        if after:
            data = git('cat-file', 'blob', after['oid'])
            entry['new_sha256'] = sha256(data)
            payload[path] = data
        changes.append(entry)
    if not changes:
        raise ValueError('No net file changes; there is nothing to deploy.')
    paths = {c['path'] for c in changes}
    for path in paths:
        parts = path.split('/')
        if any('/'.join(parts[:i]) in paths for i in range(1, len(parts))):
            raise ValueError('File/directory transitions need separate releases: ' + path)
    return {'schema': 2, 'base_commit': base, 'target_commit': target, 'changes': changes}, payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base', help='Production main commit before push')
    parser.add_argument('target', help='Cherry-picked local production commit to publish')
    parser.add_argument('--output-dir', default='packages')
    parser.add_argument('--check', action='store_true', help='Validate inputs without creating files')
    args = parser.parse_args()
    base = git('rev-parse', '--verify', f'{args.base}^{{commit}}').decode().strip()
    target = git('rev-parse', '--verify', f'{args.target}^{{commit}}').decode().strip()
    if base == target:
        raise ValueError('Base and target are the same commit.')
    if subprocess.run(['git', 'merge-base', '--is-ancestor', base, target], check=False).returncode:
        raise ValueError('Base is not an ancestor of target; refusing non-fast-forward deployment.')
    manifest, payload = plan(base, target)
    root = Path(__file__).resolve().parent
    runtime = {name: (root / name).read_bytes().replace(b'\r\n', b'\n') for name in RUNTIME}
    print(f'Base: {base}\nTarget: {target}\nChanges: {len(manifest["changes"])}')
    for change in manifest['changes']:
        print(f'{change["status"]} {change["path"]}')
    if args.check:
        print('Preflight OK; no package written.')
        return
    header = f'GIT_DEPLOY_TOOLKIT\t2\t{base}\t{target}\n'
    rows = []
    for change in manifest['changes']:
        rows.append('\t'.join(str(change[key] or '-') for key in
                              ('status', 'old_sha256', 'new_sha256', 'old_mode', 'new_mode', 'path')))
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / f'deployment_{base[:12]}_to_{target[:12]}.zip'
    created = False
    try:
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            created = True
            for path, data in payload.items():
                archive.writestr('files/' + path, data)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            archive.writestr('manifest.tsv', header + '\n'.join(rows) + '\n')
            archive.writestr('deleted_files.txt', ''.join(c['path'] + '\n' for c in manifest['changes'] if c['status'] == 'D'))
            for name, data in runtime.items():
                archive.writestr(name, data)
            archive.writestr('RELEASE.txt',
                             f'Base: {base}\nTarget: {target}\n\n'
                             'Extract into a fresh directory outside your deployment root.\n'
                             'bash check-env.sh\n'
                             'bash deploy.sh /srv/my-app --dry-run\n'
                             'bash deploy.sh /srv/my-app\n'
                             'bash rollback.sh /srv/my-app --dry-run\n'
                             'bash rollback.sh /srv/my-app\n\n'
                             'Stop jobs that may modify affected files. Keep this directory and backup/.\n'
                             'First run assumes production matches Base; all affected old bytes are checked.\n'
                             'Only declared deletions are applied. No Python or Git is needed on Linux.\n')
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    print(f'Created: {output.resolve()}')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit('Packaging failed: ' + exc.stderr.decode('utf-8', errors='replace').strip())
    except (ValueError, OSError, UnicodeError) as exc:
        sys.exit(f'Packaging failed: {exc}')
