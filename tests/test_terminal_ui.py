import argparse
import contextlib
import curses
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
ui = importlib.import_module("terminal_ui")
install = importlib.import_module("install")


class FormTests(unittest.TestCase):
    def test_first_setup_requires_token(self):
        form = ui.SetupForm(ui.Settings(url="https://crm.example.com/metrics", server_id="server-1"), False)
        with self.assertRaises(ui.InstallError):
            form.submit()
        form.values[2] = "test-token"
        settings, token = form.submit()
        self.assertEqual(settings.interfaces, ["eth*", "en*"])
        self.assertEqual(token, "test-token")

    def test_existing_settings_and_empty_token_are_preserved(self):
        settings = ui.Settings(url="https://crm.example.com/metrics", server_id="server-1", interval="20s", flush_interval="2m", interfaces=["bond0"])
        form = ui.SetupForm(settings, True)
        submitted, token = form.submit()
        self.assertEqual(submitted, settings)
        self.assertEqual(token, "")

    def test_masked_token_editing_and_navigation(self):
        form = ui.SetupForm(ui.Settings(server_id="server-1"), False)
        form.handle("\t")
        form.handle("\t")
        self.assertEqual(form.focus, 2)
        for key in "private-token":
            form.handle(key)
        self.assertEqual(form.masked_value(2), "*************")
        form.handle(curses.KEY_LEFT)
        form.handle(curses.KEY_BACKSPACE)
        self.assertEqual(form.values[2], "private-tokn")
        form.handle("\x15")
        self.assertEqual(form.values[2], "")
        self.assertEqual(form.handle("\x1b"), "cancel")

    def test_form_rejects_invalid_inputs_and_does_not_echo_token(self):
        form = ui.SetupForm(ui.Settings(url="http://crm.example.com", server_id="server-1"), False)
        form.values[2] = "PRIVATE-TOKEN"
        with self.assertRaises(ui.InstallError) as failure:
            form.submit()
        self.assertNotIn("PRIVATE-TOKEN", str(failure.exception))

    def test_submit_only_at_save_and_start(self):
        form = ui.SetupForm(ui.Settings(), False)
        for _ in range(7):
            self.assertIsNone(form.handle("\n"))
        self.assertEqual(form.handle("\n"), "submit")

    def test_backtab_wrap_and_resize_does_not_edit(self):
        form = ui.SetupForm(ui.Settings(), False)
        form.handle(curses.KEY_BTAB)
        self.assertEqual(form.focus, 7)
        old = form.values.copy()
        form.handle(curses.KEY_RESIZE)
        self.assertEqual(old, form.values)

    def test_docker_defaults_disabled_and_keyboard_can_enable_then_disable(self):
        settings = ui.Settings(url="https://crm.example.com/metrics", server_id="server-1")
        form = ui.SetupForm(settings, True)
        self.assertFalse(form.submit()[0].docker_enabled)
        for _ in range(6):
            form.handle("\t")
        self.assertEqual(form.masked_value(6), "[ Disabled ]")
        form.handle(" ")
        self.assertTrue(form.submit()[0].docker_enabled)
        self.assertFalse(settings.docker_enabled)
        form.handle(curses.KEY_LEFT)
        self.assertFalse(form.submit()[0].docker_enabled)
        form.handle(curses.KEY_RIGHT)
        self.assertTrue(form.submit()[0].docker_enabled)
        form.handle("\t")
        self.assertEqual(form.handle("\n"), "submit")

    def test_cancelling_docker_change_preserves_saved_setting(self):
        settings = ui.Settings(docker_enabled=True)
        form = ui.SetupForm(settings, True)
        form.focus = form.DOCKER_FOCUS
        form.handle(" ")
        self.assertEqual(form.handle("\x1b"), "cancel")
        self.assertTrue(settings.docker_enabled)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.installer = install.Installer(Path(self.temp.name), MagicMock())
        self.installer.directory.mkdir(parents=True)
        self.installer.unit.parent.mkdir(parents=True)
        self.installer.unit.write_text("# Managed by server-monitor\n")
        self.generation = "releases/" + "a" * 32
        self.release = self.installer.directory / self.generation
        self.release.mkdir(parents=True)
        (self.installer.directory / "current").symlink_to(self.generation)
        self.settings = ui.Settings(url="https://crm.example.com/metrics", server_id="existing", interval="20s", flush_interval="2m", interfaces=["bond0"])
        (self.release / "telegraf.conf").write_text(install.render_config(self.settings))
        (self.release / "credentials.env").write_text("SERVER_MONITOR_TOKEN=private-old-token\n")
        self.installer.preflight = MagicMock()
        self.installer.wait_active = MagicMock()
        self.controller = ui.MonitorController(self.installer)
        self.lock = patch.object(ui, "installation_lock", contextlib.nullcontext)
        self.lock.start()
        self.addCleanup(self.lock.stop)

    def test_settings_read_current_config_without_resetting_custom_defaults(self):
        self.assertEqual(self.controller.settings(), self.settings)

    def test_saved_docker_setting_is_read_and_preserved_on_reconfigure(self):
        self.settings.docker_enabled = True
        (self.release / "telegraf.conf").write_text(install.render_config(self.settings))
        saved = self.controller.settings()
        self.assertTrue(saved.docker_enabled)
        form = ui.SetupForm(saved, True)
        self.assertEqual(form.submit()[0], saved)

    def test_legacy_http_settings_can_be_read_for_upgrade(self):
        config = install.render_config(self.settings).split('[[outputs.exec]]')[0]
        config += '[[outputs.http]]\nurl = "https://crm.example.com/metrics"\n'
        (self.release / 'telegraf.conf').write_text(config)
        self.assertEqual(self.controller.settings(), self.settings)

    def test_status_distinguishes_running_stopped_failed_and_transition(self):
        cases = [("active", "running", "Running"), ("inactive", "dead", "Stopped"), ("failed", "failed", "Failed"),
                 ("activating", "auto-restart", "Starting"), ("deactivating", "stop-sigterm", "Stopping")]
        for active, sub, expected in cases:
            with self.subTest(active=active):
                self.installer.commands.run.return_value = subprocess.CompletedProcess([], 0,
                    f"LoadState=loaded\nActiveState={active}\nSubState={sub}\nUnitFileState=enabled\nMainPID=321\nMemoryCurrent=104857600\n", "")
                self.assertEqual(self.controller.snapshot()["label"], expected)

    def test_start_and_stop_only_operate_on_dedicated_service(self):
        message = self.controller.perform("start", lambda _: None)
        self.assertIn("started", message)
        self.installer.wait_active.assert_called_once()
        self.controller.perform("stop", lambda _: None)
        calls = [call.args[0] for call in self.installer.commands.run.call_args_list]
        self.assertEqual(calls, [["systemctl", "reset-failed", install.SERVICE], ["systemctl", "start", install.SERVICE], ["systemctl", "stop", install.SERVICE]])

    def test_configure_reuses_secret_when_blank_and_restores_progress(self):
        self.installer.install = MagicMock()
        self.controller.perform("configure", lambda _: None, self.settings, "")
        self.installer.install.assert_called_once_with(self.settings, "private-old-token")
        self.assertIsNone(self.installer.progress)

    def test_configure_accepts_new_token_but_invalid_settings_do_not_install(self):
        self.installer.install = MagicMock()
        self.controller.perform("configure", lambda _: None, self.settings, "new-token")
        self.installer.install.assert_called_once_with(self.settings, "new-token")
        self.installer.install.reset_mock()
        with self.assertRaises(ui.InstallError):
            self.controller.perform("configure", lambda _: None, ui.Settings(url="http://crm.example.com", server_id="id"), "new-token")
        self.installer.install.assert_not_called()

    def test_no_service_action_before_setup_or_when_unit_missing(self):
        self.installer.unit.unlink()
        with self.assertRaises(ui.InstallError):
            self.controller.perform("start", lambda _: None)
        self.installer.commands.run.assert_not_called()
        (self.installer.directory / "current").unlink()
        with self.assertRaises(ui.InstallError):
            self.controller.perform("stop", lambda _: None)
        self.installer.commands.run.assert_not_called()

    def test_checks_and_rollback_reuse_installer(self):
        self.installer.check = MagicMock()
        self.installer.rollback = MagicMock()
        self.controller.perform("check", lambda _: None)
        self.controller.perform("rollback", lambda _: None)
        self.installer.check.assert_called_once()
        self.installer.rollback.assert_called_once()

    def test_uninstall_delegates_without_requiring_a_current_release(self):
        self.installer.uninstall = MagicMock()
        (self.installer.directory / "current").unlink()
        self.controller.perform("uninstall", lambda _: None)
        self.installer.uninstall.assert_called_once()
        self.assertIsNone(self.installer.progress)

    def test_log_redaction_for_current_and_previous_credentials(self):
        previous = self.installer.directory / ("releases/" + "b" * 32)
        previous.mkdir()
        (previous / "credentials.env").write_text("SERVER_MONITOR_TOKEN=private-previous-token\n")
        (self.installer.directory / "previous").symlink_to("releases/" + "b" * 32)
        self.installer.commands.run.return_value = subprocess.CompletedProcess([], 0,
            "Authorization: private-old-token\nold: private-previous-token\n\x1b[31mwarning\x07", "")
        logs = "\n".join(self.controller.logs())
        self.assertNotIn("private-old-token", logs)
        self.assertNotIn("private-previous-token", logs)
        self.assertNotIn("\x1b", logs)
        self.assertNotIn("\x07", logs)
        self.assertIn("[REDACTED]", logs)

    def test_unknown_state_is_not_displayed_as_running(self):
        self.assertEqual(ui.service_label({}), "Unknown")
        self.assertEqual(ui.service_label({"LoadState": "not-found"}), "Not installed")
        self.assertEqual(ui.service_label({"ActiveState": "active", "SubState": "exited"}), "Active (exited)")


class FakeScreen:
    def __init__(self, height=24, width=80):
        self.height, self.width, self.text = height, width, []

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, value, limit, attr=0):
        assert y < self.height and x + limit < self.width
        self.text.append(value[:limit])

    def erase(self):
        self.text.clear()

    def refresh(self):
        pass

    def move(self, y, x):
        assert y < self.height and x < self.width


class RenderingTests(unittest.TestCase):
    def test_docker_toggle_fits_minimum_terminal_and_dashboard_shows_state(self):
        screen = FakeScreen(24, 76)
        controller = ui.DemoController()
        app = ui.TerminalUI(screen, controller, demo=True)
        app.refresh()
        app.configure()
        app.form.focus = app.form.DOCKER_FOCUS
        with patch.object(ui.curses, "curs_set"):
            app.render()
        self.assertIn("Docker monitoring", " ".join(screen.text))
        self.assertIn("[ Disabled ]", " ".join(screen.text))
        self.assertIn("Space / Left / Right", " ".join(screen.text))
        self.assertIn("grants control", " ".join(screen.text))
        self.assertIn("Save and start", " ".join(screen.text))
        controller.config.docker_enabled = True
        app.form = None
        app.refresh()
        app.render()
        self.assertIn("Enabled", " ".join(screen.text))

    def test_uninstall_defaults_to_cancel_and_escape_never_removes(self):
        app = ui.TerminalUI(FakeScreen(), ui.DemoController(), demo=True)
        app.launch = MagicMock()
        app.confirming_uninstall = True
        app.render()
        self.assertIn("Uninstall monitor", " ".join(app.screen.text))
        app.handle_uninstall("\n")
        self.assertFalse(app.confirming_uninstall)
        app.launch.assert_not_called()
        app.confirming_uninstall = True
        app.handle_uninstall("\x1b")
        app.launch.assert_not_called()

    def test_uninstall_explicit_confirmation_launches_removal(self):
        app = ui.TerminalUI(FakeScreen(), ui.DemoController(), demo=True)
        app.launch = MagicMock()
        app.confirming_uninstall = True
        app.handle_uninstall("\t")
        app.handle_uninstall("\n")
        app.launch.assert_called_once_with("uninstall")
        app.launch.reset_mock()
        app.confirming_uninstall = True
        app.handle_uninstall("y")
        app.launch.assert_called_once_with("uninstall")

    def test_dashboard_and_password_render_without_secret(self):
        screen = FakeScreen()
        app = ui.TerminalUI(screen, ui.DemoController(), demo=True)
        app.refresh()
        app.render()
        self.assertIn("RUNNING", " ".join(screen.text))
        self.assertIn("DEMO", " ".join(screen.text))
        app.configure()
        app.form.values[2] = "secret-not-for-display"
        with patch.object(ui.curses, "curs_set"):
            app.render()
        self.assertNotIn("secret-not-for-display", " ".join(screen.text))
        self.assertIn("**********************", " ".join(screen.text))

    def test_small_terminal_and_empty_log_view(self):
        screen = FakeScreen(8, 30)
        app = ui.TerminalUI(screen, ui.DemoController())
        app.render()
        self.assertIn("Resize", " ".join(screen.text))
        screen.height, screen.width = 24, 80
        app.log_lines = []
        app.render()

    def test_control_sequences_and_url_query_are_hidden(self):
        self.assertNotIn("\x1b", ui.safe_text("\x1b[31m"))
        self.assertEqual(ui.visible_url("https://crm.example.com/metrics?private=value#fragment"), "https://crm.example.com/metrics")

    def test_demo_actions_never_access_the_installer(self):
        controller = ui.DemoController()
        with patch.object(ui, "Installer", side_effect=AssertionError("must not instantiate installer")):
            controller.perform("stop", lambda _: None)
            self.assertEqual(controller.snapshot()["label"], "Stopped")
            controller.perform("start", lambda _: None)
            self.assertEqual(controller.snapshot()["label"], "Running")


if __name__ == "__main__":
    unittest.main()
