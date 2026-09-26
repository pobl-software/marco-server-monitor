import argparse
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).parents[1] / "scripts/install.py")
install = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install)


def options(**changes):
    values = dict(url="https://crm.example.com/api/metrics", server_id="server-1", interval="10s",
                  flush_interval="60s", interfaces=["eth*", "en*"], docker_enabled=False)
    values.update(changes)
    return argparse.Namespace(**values)


class ValidationTests(unittest.TestCase):
    def test_defaults_and_custom_values(self):
        install.validate_options(options())
        install.validate_options(options(url="https://[::1]:8443/metrics", interval="1m", flush_interval="1h", interfaces=["bond0"]))

    def test_bad_endpoints(self):
        for url in ["http://crm.example.com", "https://", "https://u:p@crm.example.com", "https://crm.example.com/#x",
                    "https://crm.example.com:bad", "https://crm.example.com:0", "https://crm.example.com/\nfoo", "https://crm.example.com/${TOKEN}"]:
            with self.subTest(url=url), self.assertRaises(install.InstallError):
                install.validate_options(options(url=url))

    def test_bad_ids(self):
        for identifier in ["", "../etc", "${TOKEN}", "one two", "a" * 129]:
            with self.subTest(identifier=identifier), self.assertRaises(install.InstallError):
                install.validate_options(options(server_id=identifier))

    def test_bad_intervals(self):
        for interval in ["0s", "-1s", "1.5s", "5", "1ms", "25h"]:
            with self.subTest(interval=interval), self.assertRaises(install.InstallError):
                install.validate_options(options(interval=interval))
        with self.assertRaises(install.InstallError):
            install.validate_options(options(interval="1m", flush_interval="10s"))

    def test_bad_interfaces(self):
        for interfaces in [[], [""], ["eth0\nfoo"], ['eth0", "lo'], ["${SECRET}"]]:
            with self.subTest(interfaces=interfaces), self.assertRaises(install.InstallError):
                install.validate_options(options(interfaces=interfaces))

    def test_token_character_rules(self):
        self.assertEqual(install.validate_token("abcDEF_012-./~+=="), "abcDEF_012-./~+==")
        for token in ["", "hello world", 'quote"', "abc\n", "${TOKEN}", "abc\\def", "abc=def", "x" * 8193]:
            with self.subTest(token=repr(token)), self.assertRaises(install.InstallError):
                install.validate_token(token)

    def test_protected_token_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "token"
            path.write_text("example-token\n")
            path.chmod(0o600)
            self.assertEqual(install.read_token(path), "example-token")
            path.chmod(0o644)
            with self.assertRaises(install.InstallError):
                install.read_token(path)

    def test_reject_symlink_and_multiple_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            path, link = Path(temp) / "token", Path(temp) / "link"
            path.write_text("first\nsecond\n")
            path.chmod(0o600)
            with self.assertRaises(install.InstallError):
                install.read_token(path)
            link.symlink_to(path)
            with self.assertRaises(install.InstallError):
                install.read_token(link)

    def test_noninteractive_missing_token(self):
        with patch.object(install.sys.stdin, "isatty", return_value=False), self.assertRaises(install.InstallError):
            install.read_token(None)

    def test_template_is_toml_and_token_stays_out(self):
        import tomllib
        config = install.render_config(options(url='https://crm.example.com/path?value="escaped"'))
        parsed = tomllib.loads(config)
        self.assertEqual(parsed["global_tags"]["server_id"], "server-1")
        command = parsed["outputs"]["exec"][0]["command"]
        self.assertEqual(command[-2:], ["--url", 'https://crm.example.com/path?value="escaped"'])
        self.assertEqual(set(parsed["outputs"]), {"exec"})
        self.assertNotIn("SERVER_MONITOR_TOKEN", config)
        self.assertEqual(set(parsed["inputs"]), {"cpu", "mem", "swap", "disk", "diskio", "net", "system"})
        self.assertNotIn("@@", config)

    def test_command_error_never_echoes_secrets(self):
        result = subprocess.CompletedProcess(["telegraf"], 1, stdout="secret-token", stderr="secret-token")
        with patch.object(install.subprocess, "run", return_value=result), self.assertRaises(install.InstallError) as error:
            install.Commands().run(["telegraf"], env={"SERVER_MONITOR_TOKEN": "secret-token"})
        self.assertNotIn("secret-token", str(error.exception))

    def test_docker_config_uses_container_identity_and_selected_labels(self):
        import tomllib
        parsed = tomllib.loads(install.render_config(options(docker_enabled=True)))
        docker = parsed["inputs"]["docker"][0]
        self.assertEqual(docker["endpoint"], "unix:///var/run/docker.sock")
        self.assertTrue(docker["source_tag"])
        self.assertEqual(docker["total_include"], ["cpu", "blkio", "network"])
        self.assertIn("exited", docker["container_state_include"])
        self.assertEqual(docker["docker_label_include"], ["com.docker.compose.project", "com.docker.compose.service"])
        self.assertEqual(docker["tag_env"], [])
        self.assertEqual(parsed["global_tags"]["server_id"], "server-1")

    def test_docker_toggle_rejects_non_boolean_values(self):
        for value in ("false", 1, None):
            with self.subTest(value=value), self.assertRaises(install.InstallError):
                install.validate_options(options(docker_enabled=value))

    def test_disabled_docker_does_not_require_a_socket(self):
        with patch.object(Path, "stat", side_effect=AssertionError("must not inspect Docker")):
            self.assertEqual(install.Installer().docker_groups(False), [])
        self.assertNotIn("SupplementaryGroups", install.render_unit())

    def test_docker_requires_a_socket_with_dedicated_group_access(self):
        installer = install.Installer()
        with patch.object(Path, "stat", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(install.InstallError, "Start Docker or disable"):
                installer.docker_groups(True)
        for mode, uid, gid in [(stat.S_IFREG | 0o660, 0, 995), (stat.S_IFSOCK | 0o600, 0, 995),
                               (stat.S_IFSOCK | 0o660, 0, 0), (stat.S_IFSOCK | 0o660, 1000, 995)]:
            with self.subTest(mode=mode, uid=uid, gid=gid), patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid)):
                with self.assertRaises(install.InstallError):
                    installer.docker_groups(True)
        with patch.object(Path, "stat", return_value=SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0, st_gid=995)):
            self.assertEqual(installer.docker_groups(True), [995])

    def test_collector_validation_receives_explicit_supplementary_groups(self):
        with patch.object(install.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            install.Commands().run(["telegraf", "--test"], user=100, group=100, extra_groups=[995])
            self.assertEqual(run.call_args.kwargs["extra_groups"], [995])


class FakeCommands:
    def __init__(self, root, installed=True):
        self.root, self.installed = root, installed
        self.calls = []
        self.active, self.enabled = False, False
        self.fail_validation = False
        self.fail_restart = False
        self.fail_startup = False
        self.fail_disable = False
        self.ignore_stop = False
        self.bad_key = False
        self.arch = "arm64"

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        code, out = 0, ""
        if argv[0] == "dpkg":
            out = self.arch
        elif argv[0] == "dpkg-query":
            code, out = (0, "install ok installed") if self.installed else (1, "")
        elif argv[0] == "apt-get" and argv[-1] == "telegraf":
            self.installed = True
            (self.root / "usr/bin/telegraf").touch()
        elif argv[0] == "curl":
            Path(argv[argv.index("--output") + 1]).write_text("fake key")
        elif argv[0] == "gpg":
            if "--show-keys" in argv:
                out = "fpr:::::::::" + ("BAD" if self.bad_key else install.FINGERPRINT) + ":\n"
            else:
                Path(argv[argv.index("--output") + 1]).write_text("fake dearmored key")
        elif argv[0].endswith("/telegraf"):
            if self.fail_validation:
                raise install.InstallError("validation failed")
        elif argv[0] == "systemctl":
            action = argv[1]
            if argv[-1] == "telegraf.service":
                return subprocess.CompletedProcess(argv, 0, "", "")
            if action == "is-active":
                code = 0 if self.active else 3
            elif action == "is-enabled":
                code = 0 if self.enabled else 1
            elif action == "enable":
                self.enabled = True
            elif action == "disable":
                if self.fail_disable:
                    raise install.InstallError("disable failed")
                self.enabled = False
                if "--now" in argv and not self.ignore_stop:
                    self.active = False
            elif action == "show":
                out = "active" if self.active else "inactive"
            elif action == "stop":
                self.active = False
            elif action == "start":
                self.active = True
            elif action == "restart":
                if self.fail_restart:
                    self.fail_restart = False
                    raise install.InstallError("restart failed")
                self.active = not self.fail_startup
        if code and kwargs.get("check", True):
            raise install.InstallError("fake command failed")
        return subprocess.CompletedProcess(argv, code, out, "")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ["etc", "run/systemd/system", "usr/bin", "etc/systemd/system", "etc/apt/sources.list.d"]:
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        (self.root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        (self.root / "usr/bin/telegraf").touch()
        self.commands = FakeCommands(self.root)
        self.installer = install.Installer(self.root, self.commands)
        self.patches = [patch.object(install.os, "geteuid", return_value=0),
                        patch.object(install.os, "chown"), patch.object(install.os, "fchown"),
                        patch.object(install.time, "sleep"), patch.object(install.Installer, "account")]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def do_install(self, token="first-token", **changes):
        self.installer.preflight()
        self.installer.install(options(**changes), token)
        return self.installer.release("current")

    def test_fresh_install_and_permissions(self):
        current = self.do_install()
        path = self.installer.directory / current
        self.assertEqual((path / "credentials.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((path / "telegraf.conf").stat().st_mode & 0o777, 0o640)
        self.assertEqual((path / "send_metrics.py").stat().st_mode & 0o777, 0o640)
        self.assertNotIn("first-token", (path / "telegraf.conf").read_text())
        self.assertTrue(self.commands.active)
        self.assertTrue(self.commands.enabled)
        calls = [argv for argv, _ in self.commands.calls]
        self.assertFalse(any(argv[-1] == "telegraf.service" for argv in calls))
        self.assertNotIn("first-token", str(calls))
        validation = next(kwargs for argv, kwargs in self.commands.calls if "--test" in argv)
        self.assertIn("user", validation)
        self.assertEqual(validation["env"]["SERVER_MONITOR_TOKEN"], "first-token")

    def test_sender_is_retained_with_each_release_and_checked_without_delivery(self):
        import tomllib
        first = self.do_install()
        path = self.installer.directory / first
        command = tomllib.loads((path / "telegraf.conf").read_text())["outputs"]["exec"][0]["command"]
        self.assertEqual(command[1], str(path / "send_metrics.py"))
        sender_check = next(argv for argv, _ in self.commands.calls if argv[0] == '/usr/bin/python3')
        self.assertEqual(sender_check, command + ['--check'])
        self.do_install('second-token')
        self.assertTrue((path / 'send_metrics.py').exists())
        self.installer.rollback()
        self.assertEqual(self.installer.release('current'), first)

    def test_docker_enable_disable_and_rollback_keep_permissions_with_config(self):
        import tomllib
        with patch.object(self.installer, "docker_groups", side_effect=lambda enabled: [995] if enabled else []):
            first = self.do_install(docker_enabled=True)
            self.assertIn("SupplementaryGroups=995", self.installer.unit.read_text())
            self.assertEqual(next(kwargs["extra_groups"] for argv, kwargs in self.commands.calls if "--test" in argv), [995])
            self.assertFalse(any(argv[0] in {"usermod", "chmod", "chown"} for argv, _ in self.commands.calls))
            self.commands.calls.clear()
            second = self.do_install(docker_enabled=False)
            self.assertNotIn("SupplementaryGroups", self.installer.unit.read_text())
            config = tomllib.loads((self.installer.directory / second / "telegraf.conf").read_text())
            self.assertNotIn("docker", config["inputs"])
            self.assertEqual(next(kwargs["extra_groups"] for argv, kwargs in self.commands.calls if "--test" in argv), [])
            self.installer.rollback()
            self.assertEqual(self.installer.release("current"), first)
            self.assertIn("SupplementaryGroups=995", self.installer.unit.read_text())
            self.installer.rollback()
            self.assertEqual(self.installer.release("current"), second)
            self.assertNotIn("SupplementaryGroups", self.installer.unit.read_text())

    def test_failed_docker_enable_preserves_live_host_configuration(self):
        first = self.do_install()
        old_unit = self.installer.unit.read_text()
        with self.assertRaisesRegex(install.InstallError, "Start Docker or disable"):
            self.do_install(docker_enabled=True)
        self.assertEqual(self.installer.release("current"), first)
        self.assertEqual(self.installer.unit.read_text(), old_unit)
        self.assertTrue(self.commands.active)
        self.commands.fail_validation = True
        with patch.object(self.installer, "docker_groups", side_effect=lambda enabled: [995] if enabled else []):
            with self.assertRaises(install.InstallError):
                self.do_install(docker_enabled=True)
        self.assertEqual(self.installer.release("current"), first)
        self.assertEqual(self.installer.unit.read_text(), old_unit)

    def test_docker_restart_failure_restores_service_access(self):
        first = self.do_install()
        old_unit = self.installer.unit.read_text()
        self.commands.fail_restart = True
        with patch.object(self.installer, "docker_groups", side_effect=lambda enabled: [995] if enabled else []):
            with self.assertRaises(install.InstallError):
                self.do_install(docker_enabled=True)
        self.assertEqual(self.installer.release("current"), first)
        self.assertEqual(self.installer.unit.read_text(), old_unit)
        self.assertNotIn("SupplementaryGroups", self.installer.unit.read_text())

    def test_rollback_with_changed_docker_socket_group_leaves_current_active(self):
        with patch.object(self.installer, "docker_groups", side_effect=lambda enabled: [995] if enabled else []):
            self.do_install(docker_enabled=True)
            second = self.do_install()
        with patch.object(self.installer, "docker_groups", return_value=[996]):
            with self.assertRaisesRegex(install.InstallError, "ownership changed"):
                self.installer.rollback()
        self.assertEqual(self.installer.release("current"), second)
        self.assertNotIn("SupplementaryGroups", self.installer.unit.read_text())

    def test_rollback_refuses_legacy_redirect_following_output(self):
        first = self.do_install()
        legacy = self.installer.directory / first / 'telegraf.conf'
        legacy.write_text('[[outputs.http]]\nurl = "https://crm.example.com/metrics"\n')
        second = self.do_install('second-token')
        with self.assertRaisesRegex(install.InstallError, 'secure HTTPS sender'):
            self.installer.rollback()
        self.assertEqual(self.installer.release('current'), second)

    def test_repeat_install_retains_only_two_releases_and_rollback(self):
        first = self.do_install()
        second = self.do_install("second-token", server_id="changed")
        self.assertEqual(self.installer.release("previous"), first)
        self.installer.rollback()
        self.assertEqual(self.installer.release("current"), first)
        self.assertEqual(self.installer.release("previous"), second)
        third = self.do_install("third-token")
        self.assertEqual(self.installer.release("previous"), first)
        self.assertEqual(len(list((self.installer.directory / "releases").iterdir())), 2)
        self.assertNotEqual(third, first)

    def test_validation_failure_keeps_live_release_and_backup(self):
        first = self.do_install()
        second = self.do_install("second-token")
        self.commands.fail_validation = True
        with self.assertRaises(install.InstallError):
            self.do_install("invalid-candidate")
        self.assertEqual(self.installer.release("current"), second)
        self.assertEqual(self.installer.release("previous"), first)
        self.assertTrue(self.commands.active)
        self.assertEqual(len(list((self.installer.directory / "releases").iterdir())), 2)

    def test_restart_failure_restores_release_token_unit_and_state(self):
        first = self.do_install()
        old_unit = self.installer.unit.read_text()
        self.commands.fail_restart = True
        with self.assertRaises(install.InstallError):
            self.do_install("second-token")
        self.assertEqual(self.installer.release("current"), first)
        self.assertEqual(self.installer.unit.read_text(), old_unit)
        self.assertTrue(self.commands.active)
        self.assertTrue(self.commands.enabled)

    def test_failed_first_install_removes_live_unit_and_link(self):
        self.commands.fail_restart = True
        with self.assertRaises(install.InstallError):
            self.do_install()
        self.assertIsNone(self.installer.release("current"))
        self.assertFalse(self.installer.unit.exists())
        self.assertFalse(self.commands.active)
        self.assertFalse(self.commands.enabled)

    def test_early_exit_during_startup_rolls_back(self):
        first = self.do_install()
        self.commands.fail_startup = True
        with self.assertRaises(install.InstallError):
            self.do_install("second-token")
        self.assertEqual(self.installer.release("current"), first)
        self.assertTrue(self.commands.active)

    def test_check_does_not_restart_or_send(self):
        self.do_install()
        self.commands.calls.clear()
        self.installer.check()
        self.assertEqual(len(self.commands.calls), 2)
        self.assertIn("--check", self.commands.calls[0][0])
        self.assertIn("--test", self.commands.calls[1][0])

    def test_no_previous_release(self):
        self.do_install()
        with self.assertRaises(install.InstallError):
            self.installer.rollback()

    def test_preserve_existing_package_and_config(self):
        existing = self.root / "etc/telegraf/telegraf.conf"
        existing.parent.mkdir()
        existing.write_text("existing setup")
        self.do_install()
        self.assertEqual(existing.read_text(), "existing setup")
        self.assertFalse(any(argv[0] == "apt-get" for argv, _ in self.commands.calls))

    def test_install_new_package_disables_only_new_default_service(self):
        (self.root / "usr/bin/telegraf").unlink()
        self.commands.installed = False
        self.do_install()
        self.assertIn(["systemctl", "disable", "--now", "telegraf.service"], [argv for argv, _ in self.commands.calls])
        self.assertTrue((self.root / "etc/apt/sources.list.d/server-monitor-influxdata.list").exists())

    def test_bad_repository_key_aborts_before_telegraf_install(self):
        (self.root / "usr/bin/telegraf").unlink()
        self.commands.installed = False
        self.commands.bad_key = True
        with self.assertRaises(install.InstallError):
            self.do_install()
        self.assertFalse(self.commands.installed)
        self.assertFalse((self.root / "etc/apt/sources.list.d/server-monitor-influxdata.list").exists())

    def test_amd64_supported(self):
        self.commands.arch = "amd64"
        self.installer.preflight()

    def test_unsupported_os_arch_and_systemd(self):
        self.commands.arch = "riscv64"
        with self.assertRaises(install.InstallError):
            self.installer.preflight()
        self.commands.arch = "arm64"
        (self.root / "etc/os-release").write_text('ID=debian\nVERSION_ID="12"\n')
        with self.assertRaises(install.InstallError):
            self.installer.preflight()
        (self.root / "etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        (self.root / "run/systemd/system").rmdir()
        with self.assertRaises(install.InstallError):
            self.installer.preflight()

    def test_unmanaged_directories_units_and_release_links(self):
        self.installer.unit.write_text("unrelated service")
        with self.assertRaises(install.InstallError):
            self.installer.preflight()
        self.installer.unit.unlink()
        self.installer.directory.mkdir()
        with self.assertRaises(install.InstallError):
            self.installer.preflight()
        (self.installer.directory / ".managed").write_text(install.MARKER)
        (self.installer.directory / "current").symlink_to("/etc/passwd")
        with self.assertRaises(install.InstallError):
            self.installer.preflight()

    def test_uninstall_removes_only_monitor_and_is_repeatable(self):
        self.do_install()
        self.do_install("second-token")
        shared = self.root / "etc/telegraf/telegraf.conf"
        shared.parent.mkdir()
        shared.write_text("shared config")
        repository = self.root / "etc/apt/sources.list.d/other-influxdata.list"
        repository.write_text("existing repository")
        outside = self.root / "unrelated-data"
        outside.mkdir()
        sentinel = outside / "important"
        sentinel.write_text("keep")
        (self.installer.directory / "external-link").symlink_to(outside)
        self.commands.calls.clear()
        self.installer.uninstall()
        self.assertFalse(self.installer.directory.exists())
        self.assertFalse(self.installer.unit.exists())
        self.assertFalse(self.commands.active)
        self.assertFalse(self.commands.enabled)
        self.assertEqual(shared.read_text(), "shared config")
        self.assertEqual(repository.read_text(), "existing repository")
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertTrue((self.root / "usr/bin/telegraf").exists())
        calls = [argv for argv, _ in self.commands.calls]
        self.assertIn(["systemctl", "disable", "--now", install.SERVICE], calls)
        self.assertIn(["systemctl", "reset-failed", install.SERVICE], calls)
        self.assertNotIn(["systemctl", "reset-failed"], calls)
        self.assertFalse(any(argv[0] == "apt-get" or argv[-1] == "telegraf.service" for argv in calls))
        self.commands.calls.clear()
        self.installer.uninstall()
        self.assertFalse(any(argv[0] == "systemctl" for argv, _ in self.commands.calls))

    def test_uninstall_stop_failure_keeps_files_and_credentials(self):
        current = self.do_install()
        self.commands.fail_disable = True
        with self.assertRaises(install.InstallError):
            self.installer.uninstall()
        self.assertEqual(self.installer.release("current"), current)
        self.assertTrue(self.installer.unit.exists())
        self.assertTrue((self.installer.directory / current / "credentials.env").exists())

    def test_uninstall_refuses_to_delete_files_if_service_still_running(self):
        current = self.do_install()
        self.commands.ignore_stop = True
        with self.assertRaises(install.InstallError):
            self.installer.uninstall()
        self.assertEqual(self.installer.release("current"), current)
        self.assertTrue(self.installer.unit.exists())

    def test_uninstall_rejects_unmanaged_unit_directory_and_symlinks(self):
        self.installer.directory.mkdir()
        (self.installer.directory / "unrelated").write_text("keep")
        with self.assertRaises(install.InstallError):
            self.installer.uninstall()
        self.assertTrue((self.installer.directory / "unrelated").exists())
        (self.installer.directory / ".managed").write_text(install.MARKER)
        self.installer.unit.write_text("unrelated unit")
        with self.assertRaises(install.InstallError):
            self.installer.uninstall()
        self.assertEqual(self.installer.unit.read_text(), "unrelated unit")
        self.installer.unit.unlink()
        outside = self.root / "outside-unit"
        outside.write_text("keep")
        self.installer.unit.symlink_to(outside)
        with self.assertRaises(install.InstallError):
            self.installer.uninstall()
        self.assertEqual(outside.read_text(), "keep")

    def test_uninstall_can_clean_a_managed_incomplete_install(self):
        self.installer.directory.mkdir()
        (self.installer.directory / ".managed").write_text(install.MARKER)
        self.installer.uninstall()
        self.assertFalse(self.installer.directory.exists())


if __name__ == "__main__":
    unittest.main()
