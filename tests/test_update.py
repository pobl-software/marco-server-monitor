"""Exercise updates with real source/configuration staging and fixture commands."""
import importlib
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
install = importlib.import_module("install")
update = importlib.import_module("update")
from test_install import FakeCommands, options


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("etc", "usr/bin", "etc/systemd/system", "etc/apt/sources.list.d", "opt"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "usr/bin/telegraf").touch()
        self.commands = FakeCommands(self.root)
        self.installer = install.Installer(self.root, self.commands)
        self.archive = self.root / "download.tar.gz"
        original_stat = Path.stat

        def stat_path(path, *args, **kwargs):
            if path == self.root / "var/run/docker.sock":
                import stat
                return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=995)
            return original_stat(path, *args, **kwargs)

        for context in (
            patch.object(install.os, "chown"), patch.object(install.os, "fchown"),
            patch.object(install.time, "sleep"),
            patch.object(install.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=100, pw_gid=100)),
            patch.object(Path, "stat", stat_path),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.settings = options(url="https://crm.example.com/metrics?tenant=private", server_id="kept-server",
                                interval="20s", flush_interval="2m", interfaces=["bond0"], docker_enabled=True)
        self.installer.install(self.settings, "kept-token")
        self.old = self.installer.release("current")
        self.old_unit = self.installer.unit.read_bytes()
        self.bundle()
        self.bootstrap = update.load_bootstrap()
        self.source_dir = self.root / "opt/server-monitor"
        self.launcher = self.root / "usr/local/bin/server-monitor"
        self.bootstrap.install_bundle(self.archive, self.source_dir, self.launcher)
        self.old_source = (self.source_dir / "current").readlink()
        self.old_launcher = self.launcher.read_bytes()
        self.original_run = self.commands.run
        self.fail_download = False
        self.interrupt_download = False

        def run(argv, **kwargs):
            if argv[0] == "curl" and argv[-1].endswith("/archive/main.tar.gz"):
                self.commands.calls.append((argv, kwargs))
                if self.interrupt_download:
                    raise KeyboardInterrupt
                if self.fail_download:
                    raise install.InstallError("download failed")
                shutil.copyfile(self.archive, argv[argv.index("--output") + 1])
                return subprocess.CompletedProcess(argv, 0, "", "")
            return self.original_run(argv, **kwargs)

        self.commands.run = run
        self.commands.calls.clear()

    def bundle(self, invalid=False, malicious=False):
        names = ("install.sh", "monitor.sh", "scripts/install.py", "scripts/terminal_ui.py",
                 "scripts/send_metrics.py", "scripts/update.py", "config/telegraf.conf.tmpl",
                 "systemd/server-monitor.service")
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name in names:
                data = (PROJECT / name).read_bytes()
                if invalid and name == "scripts/install.py":
                    data = b"invalid Python (\n"
                if name == "scripts/send_metrics.py":
                    data += b"\n# updated sender fixture\n"
                info = tarfile.TarInfo("project-main/" + name)
                info.size, info.mode = len(data), 0o644
                bundle.addfile(info, io.BytesIO(data))
            if malicious:
                info = tarfile.TarInfo("project-main/../../escape")
                info.size = 1
                bundle.addfile(info, io.BytesIO(b"x"))

    def assert_preserved(self, active=True):
        self.assertEqual(self.installer.release("current"), self.old)
        self.assertEqual(self.installer.unit.read_bytes(), self.old_unit)
        self.assertEqual((self.source_dir / "current").readlink(), self.old_source)
        self.assertEqual(self.launcher.read_bytes(), self.old_launcher)
        self.assertEqual(self.commands.active, active)
        self.assertTrue(self.commands.enabled)

    def test_update_stops_downloads_installs_and_restarts_with_all_saved_settings(self):
        update.update_monitor(self.installer)
        new = self.installer.release("current")
        self.assertNotEqual(new, self.old)
        saved, token = update.saved_configuration(self.installer)
        self.assertEqual(vars(saved), vars(self.settings))
        self.assertEqual(token, "kept-token")
        self.assertIn("updated sender fixture", (self.installer.directory / new / "send_metrics.py").read_text())
        self.assertNotEqual((self.source_dir / "current").readlink(), self.old_source)
        self.assertEqual(self.installer.release("previous"), self.old)
        self.assertTrue(self.commands.active)
        calls = [argv for argv, _ in self.commands.calls]
        stop = calls.index(["systemctl", "stop", install.SERVICE])
        download = next(i for i, argv in enumerate(calls) if argv[0] == "curl")
        validation = next(i for i, argv in enumerate(calls) if "--test" in argv)
        restart = calls.index(["systemctl", "restart", install.SERVICE])
        self.assertLess(stop, download)
        self.assertLess(download, validation)
        self.assertLess(validation, restart)
        self.assertNotIn("kept-token", str(calls))
        self.assertNotIn("apt-get", str(calls))

    def test_download_failure_restarts_previous_monitor(self):
        self.fail_download = True
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assert_preserved()

    def test_invalid_and_unsafe_archives_restore_previous_monitor(self):
        for invalid, malicious in ((True, False), (False, True)):
            with self.subTest(invalid=invalid, malicious=malicious):
                self.bundle(invalid=invalid, malicious=malicious)
                with self.assertRaises(install.InstallError):
                    update.update_monitor(self.installer)
                self.assert_preserved()
                self.assertFalse((self.root / "escape").exists())

    def test_collection_validation_failure_restores_source_launcher_and_service(self):
        self.commands.fail_validation = True
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assert_preserved()
        self.assertEqual(len(list((self.source_dir / "releases").iterdir())), 1)

    def test_restart_failure_restores_configuration_and_source(self):
        self.commands.fail_restart = True
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assert_preserved()

    def test_failed_update_of_stopped_monitor_keeps_it_stopped(self):
        self.commands.active = False
        self.fail_download = True
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assert_preserved(active=False)

    def test_successful_update_starts_a_previously_stopped_monitor(self):
        self.commands.active = False
        update.update_monitor(self.installer)
        self.assertTrue(self.commands.active)

    def test_update_preserves_disabled_startup_at_boot_setting(self):
        self.commands.enabled = False
        update.update_monitor(self.installer)
        self.assertTrue(self.commands.active)
        self.assertFalse(self.commands.enabled)

    def test_monitor_that_does_not_stop_is_not_updated(self):
        original_run = self.commands.run

        def run(argv, **kwargs):
            if argv == ["systemctl", "stop", install.SERVICE]:
                self.commands.calls.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, "", "")
            return original_run(argv, **kwargs)

        self.commands.run = run
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assert_preserved()
        self.assertFalse(any(argv[0] == "curl" for argv, _ in self.commands.calls))

    def test_failed_recovery_reports_that_monitor_needs_attention(self):
        original_run = self.commands.run
        self.fail_download = True

        def run(argv, **kwargs):
            if argv == ["systemctl", "start", install.SERVICE]:
                raise install.InstallError("start failed")
            return original_run(argv, **kwargs)

        self.commands.run = run
        with self.assertRaisesRegex(install.InstallError, "could not be restarted"):
            update.update_monitor(self.installer)
        self.assertFalse(self.commands.active)
        self.assertEqual(self.installer.release("current"), self.old)

    def test_interruption_restarts_previous_monitor(self):
        self.interrupt_download = True
        with self.assertRaises(KeyboardInterrupt):
            update.update_monitor(self.installer)
        self.assert_preserved()

    def test_saved_repository_is_used_with_main_branch(self):
        (self.source_dir / self.old_source / ".source-repository").write_text("owner/fork\n")
        update.update_monitor(self.installer)
        download = next(argv for argv, _ in self.commands.calls if argv[0] == "curl")
        self.assertEqual(download[-1], "https://github.com/owner/fork/archive/main.tar.gz")
        self.assertEqual((self.source_dir / "current/.source-repository").read_text(), "owner/fork\n")

    def test_unsafe_repository_or_source_is_rejected_before_stopping(self):
        (self.source_dir / self.old_source / ".source-repository").write_text("../bad\n")
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assert_preserved()
        self.assertFalse(any(argv[:2] == ["systemctl", "stop"] for argv, _ in self.commands.calls))

    def test_bad_saved_configuration_is_rejected_before_stopping(self):
        (self.installer.directory / self.old / "telegraf.conf").write_text("invalid = [\n")
        with self.assertRaises(install.InstallError):
            update.update_monitor(self.installer)
        self.assertFalse(any(argv[:2] == ["systemctl", "stop"] for argv, _ in self.commands.calls))

    def test_cleanup_failure_after_activation_does_not_revert_update(self):
        obsolete = self.source_dir / self.old_source
        self.bootstrap.install_bundle(self.archive, self.source_dir, self.launcher)
        self.old_source = (self.source_dir / "current").readlink()
        original_rmtree = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path) == obsolete:
                raise OSError("cleanup")
            return original_rmtree(path, *args, **kwargs)

        with patch.object(shutil, "rmtree", rmtree):
            update.update_monitor(self.installer)
        self.assertTrue(self.commands.active)
        self.assertNotEqual(self.installer.release("current"), self.old)
        self.assertNotEqual((self.source_dir / "current").readlink(), self.old_source)
        self.assertTrue(obsolete.exists())


if __name__ == "__main__":
    unittest.main()
