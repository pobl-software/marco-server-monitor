#!/usr/bin/env python3
"""Exercise curses with a real pseudo-terminal; no third-party test dependencies."""
import argparse
import fcntl
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import termios
import time


class TerminalSession:
    def __init__(self, project, demo=False, command=None, environment=None, controlling_terminal=False):
        self.project, self.demo = project, demo
        self.command = command
        self.environment = environment or {}
        self.controlling_terminal = controlling_terminal
        self.output = bytearray()

    def __enter__(self):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
        command = self.command or [sys.executable, str(self.project / "scripts/terminal_ui.py")]
        if self.demo:
            command.append("--demo")
        try:
            self.process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                            env=dict(os.environ, TERM="xterm-256color", **self.environment),
                                            start_new_session=not self.controlling_terminal,
                                            preexec_fn=self.claim_terminal if self.controlling_terminal else None)
        finally:
            os.close(slave)
        return self

    @staticmethod
    def claim_terminal():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    def send(self, text):
        os.write(self.master, text.encode())

    def wait_for(self, text, seconds=30):
        target = text.encode()
        offset = len(self.output)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.2)
            if readable:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                self.output.extend(data)
                if target in self.output[offset:]:
                    return
            if self.process.poll() is not None:
                break
        raise AssertionError(f"Terminal did not display expected text: {text!r} (exit {self.process.poll()}). Output withheld to protect credentials.")

    def setup(self, url, server_id, token):
        self.wait_for("First setup")
        self.send("\x15" + url + "\t\x15" + server_id + "\t" + token + "\t\x15" + "2s" + "\t\x15" + "4s" + "\t\x15" + "eth* en*" + "\t\n")
        self.wait_for("RUNNING", seconds=180)
        assert token.encode() not in self.output, "Token was echoed by the terminal"

    def quit(self):
        self.send("q")
        deadline = time.monotonic() + 10
        # Drain output while exiting: a curses redraw can otherwise fill the PTY
        # and block the child before it gets to process the quit key.
        while self.process.poll() is None and time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], 0.1)
            if readable:
                try:
                    self.output.extend(os.read(self.master, 65536))
                except OSError:
                    break
        self.process.wait(timeout=1)
        assert self.process.returncode == 0, "Terminal UI did not exit cleanly"

    def __exit__(self, *args):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        os.close(self.master)


def demo_test(project):
    with TerminalSession(project, demo=True) as session:
        session.wait_for("RUNNING")
        session.send("x")
        session.wait_for("STOPPED")
        session.send("s")
        session.wait_for("RUNNING")
        session.send("c")
        session.wait_for("Configure monitor")
        session.send("\x1b")
        session.wait_for("service controls")
        session.send("u")
        session.wait_for("Uninstall monitor")
        session.send("\n")  # The default selection is Cancel.
        session.wait_for("service controls")
        session.send("u")
        session.wait_for("Uninstall monitor")
        session.send("y")
        session.wait_for("NOT INSTALLED")
        session.quit()
    print("Real terminal preview: service controls, uninstall cancellation/confirmation and clean exit passed.")


def uninstall_test(project):
    if os.environ.get("SERVER_MONITOR_TEST_CONTAINER") != "1" or not Path("/.dockerenv").exists():
        raise SystemExit("Uninstallation is tested only inside the disposable Ubuntu container.")
    shared = Path("/etc/telegraf/telegraf.conf")
    old_shared = shared.read_bytes()
    sources = {str(p): p.read_bytes() for p in Path("/etc/apt/sources.list.d").glob("*influxdata*")}
    with TerminalSession(project) as session:
        session.wait_for("RUNNING")
        session.send("u")
        session.wait_for("Uninstall monitor")
        session.send("\n")
        session.wait_for("service controls")
        subprocess.run(["systemctl", "is-active", "--quiet", "server-monitor.service"], check=True)
        assert Path("/etc/server-monitor/current").exists(), "Cancel removed configuration"
        session.send("u")
        session.wait_for("Uninstall monitor")
        session.send("y")
        session.wait_for("NOT INSTALLED")
        session.quit()
    assert not Path("/etc/server-monitor").exists(), "Configuration/credentials/backups were not removed"
    assert not Path("/etc/systemd/system/server-monitor.service").exists(), "Service unit was not removed"
    assert subprocess.run(["systemctl", "is-active", "--quiet", "server-monitor.service"]).returncode != 0
    assert subprocess.run(["systemctl", "is-enabled", "--quiet", "server-monitor.service"], capture_output=True).returncode != 0
    assert Path("/usr/bin/telegraf").exists() and shared.read_bytes() == old_shared
    assert all(Path(name).read_bytes() == data for name, data in sources.items())
    # The single CLI command is safe to repeat after GUI removal.
    subprocess.run([sys.executable, str(project / "scripts/install.py"), "--uninstall"], check=True)
    print("Real terminal uninstall: cancellation, removal, shared-package preservation and repeat CLI removal passed.")


def installed_test(project):
    if os.environ.get("SERVER_MONITOR_TEST_CONTAINER") != "1" or not Path("/.dockerenv").exists():
        raise SystemExit("Actual service controls are tested only inside the disposable Ubuntu container.")
    # Exercise the backend through actual curses keys, not direct systemctl calls.
    with TerminalSession(project) as session:
        session.wait_for("RUNNING")
        session.send("x")
        session.wait_for("STOPPED")
        stopped = subprocess.run(["systemctl", "is-active", "--quiet", "server-monitor.service"])
        assert stopped.returncode != 0, "Stop button did not stop the service"
        session.send("s")
        session.wait_for("RUNNING")
        subprocess.run(["systemctl", "is-active", "--quiet", "server-monitor.service"], check=True)
        session.send("c")
        session.wait_for("Configure monitor")
        session.send("\x1b")
        session.wait_for("service controls")
        session.quit()
    print("Real terminal service controls: stop/start and status refresh passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", action="store_true", help="Test real service controls inside the test container")
    parser.add_argument("--uninstall", action="store_true", help="Test GUI removal inside the test container")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.uninstall:
        uninstall_test(project)
    elif args.installed:
        installed_test(project)
    else:
        demo_test(project)
