"""Test the self-contained downloader embedded in install.sh without host changes."""
import io
import fcntl
from pathlib import Path
import subprocess
import tarfile
import tempfile
import types
import unittest

PROJECT = Path(__file__).resolve().parent.parent
source = (PROJECT / 'install.sh').read_text().split("<<'SERVER_MONITOR_BOOTSTRAP_PYTHON'\n", 1)[1].split('\nSERVER_MONITOR_BOOTSTRAP_PYTHON', 1)[0]
bootstrap = types.ModuleType('bootstrap')
exec(compile(source, 'install.sh/bootstrap', 'exec'), bootstrap.__dict__)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'opt/server-monitor'
        self.directory.parent.mkdir()
        self.launcher = self.root / 'bin/server-monitor'
        self.archive = self.root / 'source.tar.gz'
        self.bundle()

    def bundle(self, extra=(), omit=None):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, contents in {
                'install.sh': '#!/bin/bash\necho installer\n',
                'monitor.sh': '#!/bin/bash\necho monitor\n',
                'scripts/install.py': 'pass\n',
                'scripts/terminal_ui.py': 'pass\n',
                'scripts/send_metrics.py': 'pass\n',
                'scripts/update.py': 'pass\n',
                'config/telegraf.conf.tmpl': '# fixture\n',
                'systemd/server-monitor.service': '# fixture\n',
            }.items():
                if name == omit:
                    continue
                data = contents.encode()
                info = tarfile.TarInfo('project-main/' + name)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
            for name, kind, link in extra:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = link
                archive.addfile(info)

    def install(self):
        return bootstrap.install_bundle(self.archive, self.directory, self.launcher)

    def test_install_and_global_command(self):
        installed = self.install()
        self.assertTrue(installed.is_file())
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o755)
        result = subprocess.run(['bash', str(self.launcher), '--check'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, 'installer\n')

    def test_repeat_keeps_two_sources(self):
        self.install()
        first = self.directory.joinpath('current').readlink()
        self.install()
        second = self.directory.joinpath('current').readlink()
        self.install()
        current = self.directory.joinpath('current').readlink()
        self.assertNotEqual(first, current)
        self.assertTrue((self.directory / second).exists())
        self.assertFalse((self.directory / first).exists())
        self.assertEqual(len(list((self.directory / 'releases').iterdir())), 2)

    def test_incomplete_bundle_preserves_previous_and_launcher(self):
        self.install()
        current = (self.directory / 'current').readlink()
        launcher = self.launcher.read_bytes()
        self.bundle(omit='scripts/terminal_ui.py')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual((self.directory / 'current').readlink(), current)
        self.assertEqual(self.launcher.read_bytes(), launcher)
        self.assertEqual(len(list((self.directory / 'releases').iterdir())), 1)

    def test_failed_activation_restores_previous_source_and_launcher(self):
        self.install()
        current = (self.directory / 'current').readlink()
        launcher = self.launcher.read_bytes()

        def reject(candidate):
            self.assertEqual(candidate.resolve(), (self.directory / 'current').resolve())
            raise ValueError('activation failed')

        with self.assertRaises(ValueError):
            bootstrap.install_bundle(self.archive, self.directory, self.launcher, activate=reject)
        self.assertEqual((self.directory / 'current').readlink(), current)
        self.assertEqual(self.launcher.read_bytes(), launcher)
        self.assertEqual(len(list((self.directory / 'releases').iterdir())), 1)

    def test_archive_rejects_traversal_absolute_links_and_special_files(self):
        for name, kind, link in (
            ('project-main/../../escape', tarfile.REGTYPE, ''),
            ('/absolute', tarfile.REGTYPE, ''),
            ('project-main/link', tarfile.SYMTYPE, '/etc'),
            ('project-main/hardlink', tarfile.LNKTYPE, 'project-main/install.sh'),
            ('project-main/fifo', tarfile.FIFOTYPE, ''),
            ('other-root/file', tarfile.REGTYPE, ''),
            ('project-main/install.sh', tarfile.REGTYPE, ''),
        ):
            with self.subTest(name=name):
                self.bundle(extra=[(name, kind, link)])
                with self.assertRaises(ValueError):
                    self.install()
                self.assertFalse(self.launcher.exists())
                self.assertFalse((self.directory / 'current').exists())

    def test_refuses_unrelated_directory(self):
        self.directory.mkdir(parents=True)
        (self.directory / 'keep').write_text('unrelated')
        with self.assertRaises(OSError):
            self.install()
        self.assertEqual((self.directory / 'keep').read_text(), 'unrelated')

    def test_refuses_unrelated_or_symlinked_command(self):
        self.launcher.parent.mkdir()
        self.launcher.write_text('unrelated')
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.launcher.read_text(), 'unrelated')
        self.launcher.unlink()
        self.launcher.symlink_to(self.archive)
        with self.assertRaises(ValueError):
            self.install()

    def test_refuses_symlinked_source_root(self):
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self.directory.symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.install()

    def test_refuses_symlinked_releases_or_unsafe_current(self):
        self.install()
        (self.directory / 'current').unlink()
        (self.directory / 'current').symlink_to('../../outside')
        with self.assertRaises(ValueError):
            self.install()
        (self.directory / 'current').unlink()
        releases = self.directory / 'releases'
        releases.rename(self.root / 'preserved')
        releases.symlink_to(self.root / 'preserved')
        with self.assertRaises(ValueError):
            self.install()

    def test_refuses_writable_command(self):
        self.install()
        self.launcher.chmod(0o777)
        with self.assertRaises(ValueError):
            self.install()

    def test_lock_prevents_concurrent_source_switch(self):
        self.install()
        previous = (self.directory / 'current').readlink()
        with (self.directory / '.download-lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.install()
        self.assertEqual((self.directory / 'current').readlink(), previous)

    def test_symlinked_lock_is_rejected(self):
        self.install()
        lock = self.directory / '.download-lock'
        lock.unlink()
        lock.symlink_to(self.archive)
        with self.assertRaises(OSError):
            self.install()
        self.assertTrue(self.archive.exists())

    def test_syntax_failure_preserves_existing_source(self):
        self.install()
        previous = (self.directory / 'current').readlink()
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name in ('install.sh', 'monitor.sh', 'scripts/install.py', 'scripts/terminal_ui.py',
                         'scripts/send_metrics.py', 'scripts/update.py', 'config/telegraf.conf.tmpl', 'systemd/server-monitor.service'):
                data = b'if: invalid Python' if name.endswith('.py') else b'# fixture\n'
                info = tarfile.TarInfo('project-main/' + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with self.assertRaises(SyntaxError):
            self.install()
        self.assertEqual((self.directory / 'current').readlink(), previous)

    def test_bad_shell_options_and_repo_fail_before_installing(self):
        for arguments in (['--repo'], ['--repo', '../unsafe'], ['--repo', 'owner/repo', '--ref', '../bad'], ['--repo', 'owner/repo', '--unknown']):
            with self.subTest(arguments=arguments):
                result = subprocess.run(['bash', '-s', '--', *arguments], input=(PROJECT / 'install.sh').read_text(), capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('Downloading', result.stdout)

    def test_standalone_help_names_default_repository_and_optional_override(self):
        result = subprocess.run(['bash', '-s', '--', '--help'], input=(PROJECT / 'install.sh').read_text(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('https://raw.githubusercontent.com/pobl-software/marco-server-monitor/main/install.sh', result.stdout)
        self.assertIn('[--ref TAG_OR_COMMIT]', result.stdout)
        self.assertIn('[--repo OWNER/REPO]', result.stdout)

    def test_local_help_still_uses_existing_cli(self):
        result = subprocess.run(['bash', str(PROJECT / 'install.sh'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--uninstall', result.stdout)
        self.assertIn('--update', result.stdout)


if __name__ == '__main__':
    unittest.main()
