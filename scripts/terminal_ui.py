#!/usr/bin/env python3
"""Terminal setup and service controls. Uses only Python's standard library."""
import argparse
import curses
from dataclasses import dataclass, field
import os
from pathlib import Path
import queue
import socket
import sys
import threading
import time
import tomllib
from urllib.parse import urlsplit, urlunsplit

from install import Installer, InstallError, SERVICE, installation_lock, validate_options, validate_token


@dataclass
class Settings:
    url: str = "https://"
    server_id: str = ""
    interval: str = "10s"
    flush_interval: str = "60s"
    interfaces: list = field(default_factory=lambda: ["eth*", "en*"])


def safe_text(value):
    """Never pass control sequences from configuration/journal data to the terminal."""
    return "".join(c if c.isprintable() and c != "\x1b" else " " for c in str(value))


def visible_url(value):
    try:
        parts = urlsplit(value)
        return safe_text(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
    except ValueError:
        return "Invalid endpoint"


def service_label(properties):
    if properties.get("LoadState") == "not-found":
        return "Not installed"
    state = properties.get("ActiveState", "unknown")
    if state == "active":
        return "Running" if properties.get("SubState") == "running" else "Active (" + properties.get("SubState", "unknown") + ")"
    return {"inactive": "Stopped", "failed": "Failed", "activating": "Starting", "deactivating": "Stopping",
            "reloading": "Reloading"}.get(state, "Unknown")


def memory_label(value):
    return f"{int(value) / 1048576:.1f} MiB" if str(value).isdigit() else "Unavailable"


class MonitorController:
    def __init__(self, installer=None):
        self.installer = installer or Installer()

    def installed(self):
        return self.installer.release("current") is not None

    def settings(self):
        release = self.installer.release("current")
        if not release:
            return Settings(server_id=socket.gethostname())
        try:
            config = tomllib.loads((self.installer.directory / release / "telegraf.conf").read_text())
            if "exec" in config["outputs"]:
                command = config["outputs"]["exec"][0]["command"]
                url = command[command.index("--url") + 1]
            else:
                # Read pre-hardening installations so Configure can upgrade them.
                url = config["outputs"]["http"][0]["url"]
            settings = Settings(url=url, server_id=config["global_tags"]["server_id"],
                                interval=config["agent"]["interval"], flush_interval=config["agent"]["flush_interval"],
                                interfaces=config["inputs"]["net"][0]["interfaces"])
            validate_options(settings)
            return settings
        except (KeyError, IndexError, TypeError, ValueError, OSError):
            raise InstallError("Cannot read installed settings. Use the command-line installer to repair the configuration.") from None

    def token(self):
        release = self.installer.release("current")
        if not release:
            raise InstallError("A bearer token is required for first setup.")
        data = (self.installer.directory / release / "credentials.env").read_text()
        if not data.startswith("SERVER_MONITOR_TOKEN="):
            raise InstallError("Cannot read the installed credential file.")
        return validate_token(data.removeprefix("SERVER_MONITOR_TOKEN=").removesuffix("\n"))

    def snapshot(self):
        installed = self.installed()
        if not installed:
            return {"installed": False, "label": "Not installed", "properties": {}, "settings": self.settings(), "error": ""}
        result = self.installer.run("systemctl", "show", SERVICE,
                                    "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID,ExecMainStartTimestamp,MemoryCurrent,Result",
                                    timeout=5)
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        error, settings = "", None
        try:
            settings = self.settings()
        except InstallError as failure:
            error = str(failure)
        return {"installed": True, "label": service_label(properties), "properties": properties, "settings": settings, "error": error}

    def perform(self, action, progress, settings=None, token=""):
        installer = self.installer
        previous_progress = installer.progress
        installer.progress = progress
        try:
            with installation_lock():
                installer.preflight()
                if action == "uninstall":
                    installer.uninstall()
                    return "Monitor uninstalled. Revoke its token in your CRM; Telegraf is retained."
                if action == "configure":
                    validate_options(settings)
                    credential = validate_token(token) if token else self.token()
                    installer.install(settings, credential)
                    return "Configuration saved and monitor started. Confirm fresh samples in your CRM."
                if not self.installed():
                    raise InstallError("Set up the monitor before using service controls.")
                if not installer.unit.is_file():
                    raise InstallError("Managed service unit is missing. Reconfigure the monitor to repair it.")
                if action == "start":
                    progress("Starting the monitor…")
                    installer.run("systemctl", "reset-failed", SERVICE, check=False)
                    installer.run("systemctl", "start", SERVICE)
                    installer.wait_active()
                    return "Monitor started."
                if action == "stop":
                    progress("Stopping the monitor…")
                    installer.run("systemctl", "stop", SERVICE)
                    return "Monitor stopped. Its startup-at-boot setting is unchanged."
                if action == "restart":
                    progress("Restarting the monitor…")
                    installer.run("systemctl", "reset-failed", SERVICE, check=False)
                    installer.run("systemctl", "restart", SERVICE)
                    installer.wait_active()
                    return "Monitor restarted."
                if action == "check":
                    progress("Checking configuration and metric collection…")
                    installer.check()
                    return "Configuration and collection passed. This check does not verify CRM delivery."
                if action == "rollback":
                    progress("Restoring the previous configuration…")
                    installer.rollback()
                    return "Previous configuration and token restored; monitor started."
                raise InstallError("Unknown monitor action.")
        finally:
            installer.progress = previous_progress

    def logs(self):
        result = self.installer.run("journalctl", "-u", SERVICE, "-n", "40", "--no-pager", "--output=short", timeout=5)
        # Redact retained tokens as well as the current one; old errors may refer to either.
        text = result.stdout
        for name in ("current", "previous"):
            release = self.installer.release(name)
            if release:
                data = (self.installer.directory / release / "credentials.env").read_text()
                credential = validate_token(data.removeprefix("SERVER_MONITOR_TOKEN=").removesuffix("\n"))
                text = text.replace(credential, "[REDACTED]")
        return [safe_text(line) for line in text.splitlines()] or ["No recent agent messages."]


class DemoController:
    """An explicit visual preview, with no system commands or filesystem changes."""
    def __init__(self):
        self.state = "Running"
        self.config = Settings(url="https://crm.example.com/api/server-metrics", server_id="production-01")

    def installed(self):
        return self.state != "Not installed"

    def settings(self):
        return self.config

    def snapshot(self):
        return {"installed": self.installed(), "label": self.state, "settings": self.config, "error": "",
                "properties": {"UnitFileState": "enabled", "MainPID": "1234" if self.state == "Running" else "0",
                               "ExecMainStartTimestamp": "Today (demo)", "MemoryCurrent": "104857600" if self.state == "Running" else "[not set]"} if self.installed() else {}}

    def perform(self, action, progress, settings=None, token=""):
        if action == "uninstall":
            self.state = "Not installed"
            return "Demo monitor uninstalled. No files or services were changed."
        if not self.installed() and action != "configure":
            raise InstallError("Set up the monitor before using service controls.")
        if action == "configure":
            self.config = settings
        if action in {"start", "restart", "configure", "rollback"}:
            self.state = "Running"
        elif action == "stop":
            self.state = "Stopped"
        return "Demo action completed. No service or files were changed."

    def logs(self):
        return ["Demo preview. No real service, credentials or log files are accessed."]


class SetupForm:
    LABELS = ("CRM HTTPS URL", "Server ID", "Bearer token", "Collect every", "Report every", "Interfaces")

    def __init__(self, settings, existing):
        self.values = [settings.url, settings.server_id, "", settings.interval, settings.flush_interval, " ".join(settings.interfaces)]
        self.existing = existing
        self.focus = 0
        self.cursor = len(self.values[0])
        self.error = ""

    def masked_value(self, index):
        return "*" * len(self.values[index]) if index == 2 else self.values[index]

    def submit(self):
        settings = Settings(url=self.values[0].strip(), server_id=self.values[1].strip(), interval=self.values[3].strip(),
                            flush_interval=self.values[4].strip(), interfaces=self.values[5].split())
        validate_options(settings)
        token = self.values[2]
        if token:
            validate_token(token)
        elif not self.existing:
            raise InstallError("Enter the per-server bearer token for first setup.")
        return settings, token

    def handle(self, key):
        if key in ("\x1b", "\x03"):
            return "cancel"
        if key in ("\t", curses.KEY_DOWN, curses.KEY_BTAB, curses.KEY_UP):
            self.focus = (self.focus + (-1 if key in (curses.KEY_BTAB, curses.KEY_UP) else 1)) % 7
            self.cursor = len(self.values[self.focus]) if self.focus < 6 else 0
        elif key in ("\n", "\r", curses.KEY_ENTER):
            if self.focus == 6:
                return "submit"
            self.focus += 1
            self.cursor = len(self.values[self.focus]) if self.focus < 6 else 0
        elif self.focus < 6:
            value = self.values[self.focus]
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if self.cursor:
                    self.values[self.focus] = value[:self.cursor - 1] + value[self.cursor:]
                    self.cursor -= 1
            elif key == curses.KEY_DC:
                self.values[self.focus] = value[:self.cursor] + value[self.cursor + 1:]
            elif key == curses.KEY_LEFT:
                self.cursor = max(0, self.cursor - 1)
            elif key == curses.KEY_RIGHT:
                self.cursor = min(len(value), self.cursor + 1)
            elif key == curses.KEY_HOME:
                self.cursor = 0
            elif key == curses.KEY_END:
                self.cursor = len(value)
            elif key == "\x15":
                self.values[self.focus] = ""
                self.cursor = 0
            elif isinstance(key, str) and key.isprintable() and len(value) < (8192 if self.focus == 2 else 4096):
                self.values[self.focus] = value[:self.cursor] + key + value[self.cursor:]
                self.cursor += len(key)
        return None


class TerminalUI:
    ACTIONS = (("s", "Start", "start"), ("x", "Stop", "stop"), ("r", "Restart", "restart"),
               ("c", "Configure", "configure"), ("k", "Check", "check"), ("b", "Rollback", "rollback"),
               ("l", "Logs", "logs"), ("u", "Uninstall", "uninstall"), ("q", "Quit", "quit"))

    def __init__(self, screen, controller, demo=False):
        self.screen, self.controller, self.demo = screen, controller, demo
        self.form = None
        self.snapshot = None
        self.notice = ""
        self.error = False
        self.events = queue.Queue()
        self.worker = None
        self.busy = False
        self.progress = ""
        self.selection = 0
        self.log_lines = None
        self.log_offset = 0
        self.last_refresh = 0
        self.colors = False
        self.confirming_uninstall = False
        self.confirm_remove = False

    def put(self, y, x, text, attribute=0, limit=None):
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width - 1:
            return
        count = min(width - x - 1, limit if limit is not None else width)
        try:
            self.screen.addnstr(y, x, safe_text(text), count, attribute)
        except curses.error:
            pass  # Wide characters/last-column writes and resize races are harmless.

    def style(self, number):
        return curses.color_pair(number) if self.colors else curses.A_BOLD

    def header(self, title):
        height, width = self.screen.getmaxyx()
        self.put(1, 3, "SERVER MONITOR" + ("  |  DEMO - no real changes" if self.demo else ""), self.style(1))
        self.put(2, 3, title)
        self.put(3, 3, "-" * (width - 6))
        self.put(height - 2, 3, "-" * (width - 6))

    def render_dashboard(self):
        self.header("Host metrics  /  Setup and service controls")
        state = self.snapshot or {"installed": False, "label": "Loading", "properties": {}, "settings": None}
        properties, settings = state["properties"], state["settings"]
        label = state["label"]
        self.put(5, 3, "Status", curses.A_DIM)
        self.put(5, 20, label.upper(), self.style(2 if label == "Running" else 3))
        rows = [("Server ID", settings.server_id if settings else "Unavailable"),
                ("CRM endpoint", visible_url(settings.url) if settings and state["installed"] else "Not configured"),
                ("Collection", settings.interval if settings else "Unavailable"),
                ("Reporting", settings.flush_interval if settings else "Unavailable"),
                ("Interfaces", "  ".join(settings.interfaces) if settings else "Unavailable"),
                ("Start at boot", properties.get("UnitFileState", "Not configured")),
                ("Agent PID / RAM", properties.get("MainPID", "-") + " / " + memory_label(properties.get("MemoryCurrent", ""))),
                ("Started", properties.get("ExecMainStartTimestamp") or "-")]
        for index, (name, value) in enumerate(rows):
            self.put(7 + index, 3, name, curses.A_DIM)
            self.put(7 + index, 20, value)
        self.put(16, 3, "Service status does not confirm delivery to your CRM.", curses.A_DIM)
        height, width = self.screen.getmaxyx()
        self.put(18, 3, self.progress if self.busy else self.notice or state.get("error", ""), self.style(3) if self.error else 0)
        x, y = 3, height - 5
        for index, (key, name, _) in enumerate(self.ACTIONS):
            label = f" {key.upper()} {name} "
            if x + len(label) > width - 3:
                x, y = 3, y + 1
            self.put(y, x, label, curses.A_REVERSE if index == self.selection else 0)
            x += len(label) + 1
        self.put(height - 1, 3, "Working… please wait" if self.busy else "Arrows / Tab to select   Enter to run   Q to close")

    def render_form(self):
        form = self.form
        self.header("Configure monitor" if form.existing else "First setup  /  Connect this server to your CRM")
        height, width = self.screen.getmaxyx()
        self.put(5, 3, "Enter your endpoint and identity. The bearer token stays hidden.")
        for index, label in enumerate(form.LABELS):
            y = 7 + index * 2
            self.put(y, 3, label, curses.A_BOLD if form.focus == index else curses.A_DIM)
            value = form.masked_value(index)
            available = width - 25
            offset = max(0, form.cursor - available + 1) if form.focus == index else 0
            shown = value[offset:offset + available]
            if not value and index == 2:
                shown = "(blank keeps existing token)" if form.existing else "(required)"
            self.put(y, 22, shown.ljust(available), curses.A_REVERSE if form.focus == index else 0, limit=available)
        self.put(19, 3, "Interfaces: space-separated names/globs, e.g. eth* en* or bond0", curses.A_DIM)
        self.put(20, 3, form.error, self.style(3))
        self.put(height - 3, 3, "  Save and start monitor  ", curses.A_REVERSE if form.focus == 6 else curses.A_BOLD)
        self.put(height - 1, 3, "Tab / arrows to move   Enter to continue/save   Esc to cancel   Ctrl-U clear")
        try:
            curses.curs_set(1 if form.focus < 6 else 0)
            if form.focus < 6:
                available = width - 25
                offset = max(0, form.cursor - available + 1)
                self.screen.move(7 + form.focus * 2, 22 + min(form.cursor - offset, available - 1))
        except curses.error:
            pass

    def render_logs(self):
        self.header("Recent agent messages  /  Tokens redacted")
        height, _ = self.screen.getmaxyx()
        for index, line in enumerate(self.log_lines[self.log_offset:self.log_offset + height - 7]):
            self.put(5 + index, 3, line)
        self.put(height - 1, 3, "Up / Down to scroll   Esc / Q to return   L to refresh")

    def render_uninstall(self):
        self.header("Uninstall monitor")
        height, _ = self.screen.getmaxyx()
        self.put(6, 3, "Remove this server's monitor?", self.style(3))
        self.put(8, 3, "This stops monitoring and disables startup at boot.")
        self.put(10, 3, "It deletes the monitor's configuration, credentials and backups.")
        self.put(12, 3, "Telegraf, its APT repository and other services are kept.")
        self.put(14, 3, "Revoke the token in your CRM and delete any source token file separately.")
        self.put(height - 4, 3, "  Cancel  ", curses.A_REVERSE if not self.confirm_remove else 0)
        self.put(height - 4, 18, "  Uninstall monitor  ", curses.A_REVERSE if self.confirm_remove else 0)
        self.put(height - 1, 3, "Tab / arrows to choose   Enter to confirm   Y remove   Esc / N cancel")

    def handle_uninstall(self, key):
        if key in ("\x1b", "\x03", "n", "N", "q", "Q"):
            self.confirming_uninstall = False
        elif key in ("\t", curses.KEY_BTAB, curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP, curses.KEY_DOWN):
            self.confirm_remove = not self.confirm_remove
        elif key in ("y", "Y", "\n", "\r", curses.KEY_ENTER):
            remove = key in ("y", "Y") or self.confirm_remove
            self.confirming_uninstall = False
            if remove:
                self.launch("uninstall")

    def render(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 24 or width < 76:
            self.put(0, 0, "Resize terminal to at least 76 columns x 24 rows.")
            self.put(2, 0, "Esc / Q to cancel or close. Working tasks finish before closing.")
        elif self.confirming_uninstall:
            self.render_uninstall()
        elif self.form:
            self.render_form()
        elif self.log_lines is not None:
            self.render_logs()
        else:
            self.render_dashboard()
        self.screen.refresh()

    def refresh(self):
        try:
            self.snapshot = self.controller.snapshot()
        except (InstallError, OSError) as error:
            self.notice, self.error = str(error), True
        self.last_refresh = time.monotonic()

    def configure(self):
        try:
            self.form = SetupForm(self.controller.settings(), self.controller.installed())
        except (InstallError, OSError) as error:
            self.notice, self.error = str(error), True

    def launch(self, action, settings=None, token=""):
        self.busy, self.error = True, False
        self.progress = "Working…"

        def job():
            try:
                message = self.controller.perform(action, lambda text: self.events.put(("progress", text)), settings, token)
                self.events.put(("done", message))
            except (InstallError, OSError) as error:
                self.events.put(("error", str(error)))
            except Exception:
                self.events.put(("error", "Operation failed. No diagnostic output was printed to protect credentials."))

        # Do not allow leaving while installation is underway; losing an SSH session
        # still leaves the installer responsible for its normal rollback behaviour.
        self.worker = threading.Thread(target=job, daemon=False)
        self.worker.start()

    def drain_events(self):
        while not self.events.empty():
            kind, text = self.events.get_nowait()
            if kind == "progress":
                self.progress = text
            else:
                self.notice, self.error, self.busy = text, kind == "error", False
                self.last_refresh = 0

    def show_logs(self):
        try:
            self.log_lines = self.controller.logs()
            self.log_offset = max(0, len(self.log_lines) - (self.screen.getmaxyx()[0] - 7))
        except (InstallError, OSError) as error:
            self.notice, self.error = str(error), True

    def loop(self):
        self.screen.keypad(True)
        self.screen.timeout(200)
        curses.noecho()
        try:
            curses.curs_set(0)
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                for index, foreground in ((1, curses.COLOR_CYAN), (2, curses.COLOR_GREEN), (3, curses.COLOR_YELLOW)):
                    curses.init_pair(index, foreground, -1)
                self.colors = True
        except curses.error:
            pass
        self.refresh()
        if self.snapshot and not self.snapshot["installed"]:
            self.configure()
        while True:
            self.drain_events()
            if not self.busy and time.monotonic() - self.last_refresh >= 2:
                self.refresh()
            if not self.form:
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
            self.render()
            try:
                key = self.screen.get_wch()
            except curses.error:
                continue
            if self.busy or key == curses.KEY_RESIZE:
                continue
            height, width = self.screen.getmaxyx()
            if height < 24 or width < 76:
                if key in ("q", "Q", "\x1b", "\x03"):
                    if self.confirming_uninstall:
                        self.confirming_uninstall = False
                    elif self.form:
                        self.form = None
                    else:
                        break
                continue
            if self.confirming_uninstall:
                self.handle_uninstall(key)
                continue
            if self.form:
                result = self.form.handle(key)
                if result == "cancel":
                    self.form = None
                elif result == "submit":
                    try:
                        settings, token = self.form.submit()
                        self.form = None
                        self.launch("configure", settings, token)
                    except InstallError as error:
                        self.form.error = str(error)
                continue
            if self.log_lines is not None:
                if key in ("q", "Q", "\x1b", "\x03"):
                    self.log_lines = None
                elif key == curses.KEY_UP:
                    self.log_offset = max(0, self.log_offset - 1)
                elif key == curses.KEY_DOWN:
                    self.log_offset = min(max(0, len(self.log_lines) - 1), self.log_offset + 1)
                elif key in ("l", "L"):
                    self.show_logs()
                continue
            if key in (curses.KEY_RIGHT, curses.KEY_DOWN, "\t", curses.KEY_LEFT, curses.KEY_UP, curses.KEY_BTAB):
                self.selection = (self.selection + (-1 if key in (curses.KEY_LEFT, curses.KEY_UP, curses.KEY_BTAB) else 1)) % len(self.ACTIONS)
                continue
            action = self.ACTIONS[self.selection][2] if key in ("\n", "\r", curses.KEY_ENTER) else next(
                (action for hotkey, _, action in self.ACTIONS if isinstance(key, str) and key.lower() == hotkey), None)
            if key in ("\x1b", "\x03") or action == "quit":
                break
            if action == "configure":
                self.configure()
            elif action == "logs":
                self.show_logs()
            elif action == "uninstall":
                self.confirming_uninstall = True
                self.confirm_remove = False
            elif action:
                self.launch(action)


def main():
    parser = argparse.ArgumentParser(description="Interactive setup and controls for server-monitor.service.")
    parser.add_argument("--demo", action="store_true", help="Preview the terminal UI locally; no installation or service changes")
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get("TERM") in (None, "", "dumb"):
            raise InstallError("Open this interface in an interactive terminal (SSH: use ssh -t).")
        controller = DemoController() if args.demo else MonitorController()
        if not args.demo:
            controller.installer.preflight()
        curses.wrapper(lambda screen: TerminalUI(screen, controller, args.demo).loop())
    except (InstallError, OSError, curses.error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # curses.wrapper restores the terminal, and in-flight jobs finish before exit.
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
