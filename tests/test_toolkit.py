"""End-to-end tests against disposable application repositories and deployment roots."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

TOOLKIT = Path(__file__).resolve().parents[1]
BASH = shutil.which('bash')
PWSH = os.environ.get('TOOLKIT_PWSH') or shutil.which('pwsh')


def run(*args, cwd=None, env=None, success=True):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if success and result.returncode:
        raise AssertionError(f'{args}\n{result.stdout}')
    if not success and not result.returncode:
        raise AssertionError(f'Expected rejection: {args}\n{result.stdout}')
    return result.stdout.strip()


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in root.rglob('*') if p.is_file()}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='git-toolkit-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'application'
        self.repo.mkdir()
        self.target = self.root / 'production'
        self.target.mkdir()
        run('git', 'init', '-b', 'main', cwd=self.repo)
        run('git', 'config', 'user.name', 'Toolkit Test', cwd=self.repo)
        run('git', 'config', 'user.email', 'test@example.com', cwd=self.repo)
        for name, data in {'modify.txt': b'old\n', 'delete.txt': b'deleted\n',
                           'old name.txt': b'rename\n', 'mode.sh': b'#!/bin/sh\nexit 0\n',
                           'unchanged.txt': b'unchanged\n'}.items():
            (self.repo / name).write_bytes(data)
            (self.target / name).write_bytes(data)
        (self.target / 'modify.txt').chmod(0o750)
        (self.target / 'runtime.dat').write_bytes(b'preserve untracked runtime data')
        self.commit('base')
        self.base = self.head()
        self.before = snapshot(self.target)

    def commit(self, message):
        run('git', 'add', '-A', cwd=self.repo)
        run('git', 'commit', '-m', message, cwd=self.repo)

    def head(self):
        return run('git', 'rev-parse', 'HEAD', cwd=self.repo)

    def changes(self):
        (self.repo / 'modify.txt').write_bytes(b'new\n')
        (self.repo / 'delete.txt').unlink()
        (self.repo / 'old name.txt').rename(self.repo / 'new name.txt')
        (self.repo / 'scripts with spaces').mkdir()
        (self.repo / 'scripts with spaces/中文.bin').write_bytes(bytes(range(256)))
        (self.repo / 'mode.sh').chmod(0o755)
        run('git', 'add', '-A', cwd=self.repo)
        run('git', 'update-index', '--chmod=+x', 'mode.sh', cwd=self.repo)
        run('git', 'commit', '-m', 'production changes', cwd=self.repo)
        self.dest = self.head()

    def package(self):
        self.out = self.root / 'packages'
        run(sys.executable, TOOLKIT / 'package.py', self.base, self.dest,
            '--output-dir', self.out, cwd=self.repo)
        self.archive = next(self.out.glob('*.zip'))
        self.release = self.root / 'release'
        with zipfile.ZipFile(self.archive) as archive:
            self.assertIsNone(archive.testzip())
            self.assertNotIn('deploy_engine.py', archive.namelist())
            archive.extractall(self.release)
        return self.release

    def deploy(self, *args, success=True, env=None):
        return run(BASH, self.release / 'deploy.sh', self.target, *args, success=success, env=env)

    def rollback(self, *args, success=True, env=None):
        return run(BASH, self.release / 'rollback.sh', self.target, *args, success=success, env=env)

    def minimal_env(self, hasher='shasum'):
        bindir = self.root / ('bin-' + hasher)
        bindir.mkdir(exist_ok=True)
        for name in ('cp', 'mv', 'rm', 'mkdir', 'rmdir', 'chmod', 'mktemp', 'stat', hasher):
            if not (bindir / name).exists():
                if name == 'shasum' and sys.platform == 'darwin':
                    # Apple's Perl launcher depends on the script's original path.
                    (bindir / name).write_text('#!/bin/bash\nexec /usr/bin/shasum "$@"\n')
                    (bindir / name).chmod(0o755)
                else:
                    (bindir / name).symlink_to(shutil.which(name))
        env = os.environ.copy()
        env['PATH'] = str(bindir)
        return env, bindir


class ShellDeploymentTests(Fixture):
    def setUp(self):
        super().setUp()
        self.changes()
        self.package()

    def test_deploy_dry_run_and_rollback_without_python(self):
        env, _ = self.minimal_env()
        original_release = snapshot(self.release)
        self.assertIn('DRY RUN ONLY', self.deploy('--dry-run', env=env))
        self.assertEqual(self.before, snapshot(self.target))
        self.assertEqual(original_release, snapshot(self.release))
        self.assertIn('DEPLOYMENT COMPLETE', self.deploy(env=env))
        self.assertEqual(b'new\n', (self.target / 'modify.txt').read_bytes())
        self.assertFalse((self.target / 'delete.txt').exists())
        self.assertFalse((self.target / 'old name.txt').exists())
        self.assertEqual(b'rename\n', (self.target / 'new name.txt').read_bytes())
        self.assertEqual(bytes(range(256)), (self.target / 'scripts with spaces/中文.bin').read_bytes())
        self.assertEqual(0o750, stat.S_IMODE((self.target / 'modify.txt').stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE((self.target / 'mode.sh').stat().st_mode))
        self.assertEqual(self.dest, (self.target / '.deployment-state/current_commit.txt').read_text().strip())
        deployed = snapshot(self.target)
        backup = snapshot(self.release / 'backup')
        self.assertIn('DRY RUN ONLY', self.rollback('--dry-run', env=env))
        self.assertEqual(deployed, snapshot(self.target))
        self.assertEqual(backup, snapshot(self.release / 'backup'))
        self.assertIn('ROLLBACK COMPLETE', self.rollback(env=env))
        self.assertEqual(self.before, snapshot(self.target))
        self.assertFalse((self.target / '.deployment-state/lock').exists())
        self.deploy(success=False)

    def test_openssl_checksum_fallback(self):
        env, _ = self.minimal_env('openssl')
        self.assertIn('via openssl', self.deploy('--dry-run', env=env))
        self.assertEqual(self.before, snapshot(self.target))

    def test_missing_commands_fail_without_writes(self):
        env = dict(os.environ, PATH='')
        self.assertIn('MISSING', self.deploy(success=False, env=env))
        self.assertEqual(self.before, snapshot(self.target))
        self.assertFalse((self.release / 'backup').exists())

    def test_existing_version_restored_on_rollback(self):
        state = self.target / '.deployment-state'
        state.mkdir()
        (state / 'current_commit.txt').write_text(self.base + '\n')
        before = snapshot(self.target)
        self.deploy()
        self.rollback()
        self.assertEqual(before, snapshot(self.target))

    def test_bad_version_and_lock_rejected(self):
        state = self.target / '.deployment-state'
        state.mkdir()
        marker = state / 'current_commit.txt'
        marker.write_text('f' * 40 + '\n')
        before = snapshot(self.target)
        self.deploy('--dry-run', success=False)
        self.assertEqual(before, snapshot(self.target))
        marker.write_text(self.base + '\n')
        (state / 'lock').mkdir()
        before = snapshot(self.target)
        self.deploy(success=False)
        self.assertEqual(before, snapshot(self.target))
        self.assertFalse((self.release / 'backup').exists())

    def test_old_content_and_new_collision_rejected(self):
        (self.target / 'modify.txt').write_text('local edits')
        before = snapshot(self.target)
        self.deploy(success=False)
        self.assertEqual(before, snapshot(self.target))
        (self.target / 'modify.txt').write_bytes(b'old\n')
        (self.target / 'new name.txt').write_text('local data')
        before = snapshot(self.target)
        self.deploy(success=False)
        self.assertEqual(before, snapshot(self.target))

    def test_payload_corruption_rejected_before_writes(self):
        (self.release / 'files/modify.txt').write_text('corrupted')
        self.deploy(success=False)
        self.assertEqual(self.before, snapshot(self.target))
        self.assertFalse((self.release / 'backup').exists())

    def test_symlink_payload_or_target_rejected(self):
        payload = self.release / 'files/modify.txt'
        data = payload.read_bytes()
        payload.unlink()
        payload.symlink_to(self.repo / 'modify.txt')
        self.deploy(success=False)
        payload.unlink()
        payload.write_bytes(data)
        (self.target / 'new name.txt').symlink_to(self.target / 'unchanged.txt')
        self.deploy(success=False)
        self.assertFalse((self.release / 'backup').exists())

    def test_state_symlink_and_reserved_manifest_rejected(self):
        state = self.target / '.deployment-state'
        state.mkdir()
        (state / 'current_commit.txt').symlink_to(self.target / 'unchanged.txt')
        self.deploy(success=False)
        (state / 'current_commit.txt').unlink()
        manifest = self.release / 'manifest.tsv'
        manifest.write_text(manifest.read_text().replace('modify.txt', '.git/config'))
        self.deploy(success=False)
        self.assertEqual(self.before, snapshot(self.target))

    def test_bad_options_and_overlapping_release_rejected(self):
        self.deploy('--unknown', success=False)
        self.deploy('--dry-run', '--extra', success=False)
        run(BASH, self.release / 'deploy.sh', self.release, success=False)
        self.assertEqual(self.before, snapshot(self.target))

    def test_modified_deployed_file_blocks_rollback(self):
        self.deploy()
        (self.target / 'modify.txt').write_text('post-deploy edits')
        before = snapshot(self.target)
        self.rollback(success=False)
        self.assertEqual(before, snapshot(self.target))

    def test_all_backups_checked_before_any_rollback_write(self):
        self.deploy()
        (self.release / 'backup/files/modify.txt').write_text('corrupt backup')
        before = snapshot(self.target)
        self.rollback(success=False)
        self.assertEqual(before, snapshot(self.target))

    def test_interrupted_atomic_write_can_be_rolled_back(self):
        env, bindir = self.minimal_env()
        mv = bindir / 'mv'
        mv.unlink()
        mv.write_text('#!/bin/bash\nfor arg; do last=$arg; done\n'
                      'if [[ $last == */modify.txt ]]; then exit 55; fi\n'
                      f'exec {shutil.which("mv")} "$@"\n')
        mv.chmod(0o755)
        self.deploy(success=False, env=env)
        self.assertFalse((self.target / '.deployment-state/lock').exists())
        self.assertIn('ROLLBACK COMPLETE', self.rollback())
        self.assertEqual(self.before, snapshot(self.target))


class PackageTests(Fixture):
    def test_check_does_not_write_and_archive_never_overwrites(self):
        self.changes()
        output = self.root / 'not-created'
        run(sys.executable, TOOLKIT / 'package.py', self.base, self.dest, '--check', '--output-dir', output, cwd=self.repo)
        self.assertFalse(output.exists())
        self.package()
        before = self.archive.read_bytes()
        run(sys.executable, TOOLKIT / 'package.py', self.base, self.dest, '--output-dir', self.out, cwd=self.repo, success=False)
        self.assertEqual(before, self.archive.read_bytes())

    def test_changed_symlink_rejected(self):
        (self.repo / 'link').symlink_to('modify.txt')
        self.commit('symlink')
        run(sys.executable, TOOLKIT / 'package.py', self.base, 'HEAD', '--check', cwd=self.repo, success=False)

    def test_file_directory_transition_rejected(self):
        (self.repo / 'modify.txt').unlink()
        (self.repo / 'modify.txt').mkdir()
        (self.repo / 'modify.txt/child').write_text('new')
        self.commit('file to directory')
        run(sys.executable, TOOLKIT / 'package.py', self.base, 'HEAD', '--check', cwd=self.repo, success=False)


@unittest.skipUnless(PWSH, 'PowerShell is not installed; set TOOLKIT_PWSH to run publish integration tests')
class PublishTests(Fixture):
    def setUp(self):
        super().setUp()
        self.remote = self.root / 'gitlab.git'
        run('git', 'init', '--bare', self.remote)
        run('git', 'remote', 'add', 'origin', self.remote, cwd=self.repo)
        run('git', 'push', 'origin', 'main', cwd=self.repo)
        self.output = self.root / 'published'
        self.bin = self.root / 'windows-command-shims'
        self.bin.mkdir()
        (self.bin / 'python').symlink_to(sys.executable)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def publish(self, success=True, env=None):
        return run(PWSH, '-NoLogo', '-NoProfile', '-File', TOOLKIT / 'publish.ps1',
                   '-RepoPath', self.repo, '-Remote', 'origin', '-Branch', 'main',
                   '-OutputDir', self.output, success=success, env=env or self.env)

    def remote_head(self):
        return run('git', '--git-dir=' + str(self.remote), 'rev-parse', 'refs/heads/main')

    def test_cherry_pick_from_etl_then_publish_and_deploy(self):
        etl = self.root / 'linux-etl'
        run('git', 'clone', self.repo, etl)
        run('git', 'config', 'user.name', 'ETL Developer', cwd=etl)
        run('git', 'config', 'user.email', 'developer@example.com', cwd=etl)
        (etl / 'selected.sh').write_text('selected change\n')
        run('git', 'add', '.', cwd=etl); run('git', 'commit', '-m', 'selected ETL change', cwd=etl)
        selected = run('git', 'rev-parse', 'HEAD', cwd=etl)
        (etl / 'unselected.sh').write_text('not selected\n')
        run('git', 'add', '.', cwd=etl); run('git', 'commit', '-m', 'unselected ETL change', cwd=etl)
        run('git', 'remote', 'add', 'etl', etl, cwd=self.repo)
        run('git', 'fetch', 'etl', 'main', cwd=self.repo)
        run('git', 'switch', '-c', 'local-production', cwd=self.repo)
        run('git', 'cherry-pick', selected, cwd=self.repo)
        self.assertIn('Publish complete', self.publish())
        self.assertEqual(self.head(), self.remote_head())
        archive = next(self.output.glob('*.zip'))
        self.release = self.root / 'release'
        with zipfile.ZipFile(archive) as z:
            z.extractall(self.release)
        manifest = json.loads((self.release / 'manifest.json').read_text())
        self.assertEqual(['selected.sh'], [c['path'] for c in manifest['changes']])
        self.assertEqual(self.base, manifest['base_commit'])
        self.assertIn('DEPLOYMENT COMPLETE', self.deploy())
        self.assertTrue((self.target / 'selected.sh').exists())
        self.assertFalse((self.target / 'unselected.sh').exists())
        self.rollback()
        self.assertEqual(self.before, snapshot(self.target))
        self.assertIn('Nothing new', self.publish())

    def test_dirty_tree_is_not_pushed(self):
        self.changes()
        (self.repo / 'modify.txt').write_text('uncommitted')
        self.publish(success=False)
        self.assertEqual(self.base, self.remote_head())
        self.assertFalse(self.output.exists())

    def test_unsupported_payload_is_rejected_before_push(self):
        (self.repo / 'link').symlink_to('modify.txt')
        self.commit('unsupported symlink')
        self.publish(success=False)
        self.assertEqual(self.base, self.remote_head())

    def test_packaging_failure_after_push_can_be_rebuilt_offline(self):
        self.changes()
        python = self.bin / 'python'
        python.unlink()
        python.write_text('#!/bin/bash\ncase " $* " in *" --check "*) '
                          f'exec {sys.executable} "$@" ;; *) exit 55 ;; esac\n')
        python.chmod(0o755)
        self.assertIn('Push succeeded but packaging failed', self.publish(success=False))
        self.assertEqual(self.dest, self.remote_head())
        record = json.loads(next(self.output.glob('*.json')).read_text(encoding='utf-8-sig'))
        self.assertEqual('pushed', record['status'])
        self.assertEqual(self.base, record['base_commit'])
        python.unlink(); python.symlink_to(sys.executable)
        run('git', 'remote', 'remove', 'origin', cwd=self.repo)
        run(PWSH, '-NoLogo', '-NoProfile', '-File', TOOLKIT / 'package.ps1', '-RepoPath', self.repo,
            '-Base', self.base, '-Target', self.dest, '-OutputDir', self.output, env=self.env)
        self.assertEqual(1, len(list(self.output.glob('*.zip'))))

    def test_concurrent_remote_update_rejected_by_lease(self):
        self.changes()
        race = run('git', 'commit-tree', 'HEAD^{tree}', '-p', self.base, '-m', 'other publisher', cwd=self.repo)
        run('git', '--git-dir=' + str(self.remote), 'fetch', self.repo, race)
        python = self.bin / 'python'
        python.unlink()
        python.write_text('#!/bin/bash\n' + f'{sys.executable} "$@"\nresult=$?\n'
                          + 'case " $* " in *" --check "*) '
                          + f'git --git-dir="{self.remote}" update-ref refs/heads/main {race} ;; esac\n'
                          + 'exit "$result"\n')
        python.chmod(0o755)
        self.publish(success=False)
        self.assertEqual(race, self.remote_head())
        self.assertFalse(list(self.output.glob('*.zip')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
