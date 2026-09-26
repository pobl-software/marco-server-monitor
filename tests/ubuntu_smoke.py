#!/usr/bin/env python3
"""Real installation/service test, restricted to the disposable test container."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time

from terminal_smoke import TerminalSession, installed_test


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def wait_for(predicate, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for fresh service delivery")


def main():
    if os.environ.get("SERVER_MONITOR_TEST_CONTAINER") != "1" or not Path("/.dockerenv").exists() or os.geteuid() != 0:
        raise SystemExit("Run only inside the disposable Ubuntu test container; this test changes its packages and system trust.")
    project = Path(__file__).resolve().parents[1]
    assert Path("/run/systemd/system").is_dir(), "systemd is not running"
    packaged = Path("/etc/telegraf/telegraf.conf")
    original_packaged = packaged.read_bytes() if packaged.exists() else None
    packaged_state = subprocess.run(["systemctl", "is-enabled", "telegraf.service"], capture_output=True).stdout
    samples = []
    errors = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                token = self.headers.get("Authorization")
                expected_id = {"Bearer smoke-token-one": "smoke-one", "Bearer smoke-token-two": "smoke-two"}.get(token)
                assert expected_id, "unexpected bearer token"
                assert self.headers.get("Content-Type") == "application/json", "invalid content type"
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert payload["schema_version"] == 1, "unexpected schema version"
                assert payload["server_id"] == expected_id, "config/token pair mismatch"
                assert payload["hostname"] and payload["samples"], "missing identity or samples"
                samples.extend(dict(sample, server_id=payload["server_id"]) for sample in payload["samples"])
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception as error:
                errors.append(str(error))
                self.send_error(400)

    with tempfile.TemporaryDirectory(prefix="smoke-") as temp:
        directory = Path(temp)
        cert, key = directory / "ca.crt", directory / "key.pem"
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert))
        trust = Path("/usr/local/share/ca-certificates/server-monitor-smoke.crt")
        trust.write_bytes(cert.read_bytes())
        run("update-ca-certificates")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        data_mount, ignored_mount = Path("/srv/monitor-test-data"), Path("/srv/monitor-test-tmp")
        data_mount.mkdir(exist_ok=True)
        ignored_mount.mkdir(exist_ok=True)
        image = directory / "disk.img"
        with image.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
        run("mkfs.ext4", "-q", "-F", str(image))
        run("mount", "-o", "loop", str(image), str(data_mount))
        run("mount", "-t", "tmpfs", "tmpfs", str(ignored_mount))
        token_file = directory / "token"

        def install(identifier, token):
            token_file.write_text(token)
            token_file.chmod(0o600)
            return run("python3", str(project / "scripts/install.py"), "--url", f"https://127.0.0.1:{server.server_port}/metrics",
                       "--server-id", identifier, "--token-file", str(token_file), "--interval", "2s", "--flush-interval", "4s")

        try:
            # A new installation goes through the interactive form, including a hidden token.
            if not Path("/etc/server-monitor/current").exists():
                with TerminalSession(project) as session:
                    session.setup(f"https://127.0.0.1:{server.server_port}/metrics", "smoke-one", "smoke-token-one")
                    session.quit()
            else:
                install("smoke-one", "smoke-token-one")
            first = os.readlink("/etc/server-monitor/current")
            installed_test(project)
            wait_for(lambda: any("cpu" in s["host"] and s["server_id"] == "smoke-one" for s in samples))
            assert not errors, errors
            disks = [row for sample in samples for row in sample["host"].get("disks", [])]
            assert any(row["mount"] == str(data_mount) for row in disks), "Separate ext4 mount was not monitored"
            assert not any(row["mount"] == str(ignored_mount) for row in disks), "tmpfs was not filtered"
            assert all(row["filesystem"] not in {"overlay", "tmpfs", "squashfs"} for row in disks)
            networks = [row for sample in samples for row in sample["host"].get("network", [])]
            assert networks and all(row["interface"].startswith(("eth", "en")) for row in networks)
            run("python3", str(project / "scripts/install.py"), "--check")
            current = Path("/etc/server-monitor/current")
            assert (current / "credentials.env").stat().st_mode & 0o777 == 0o600
            forbidden = subprocess.run(["runuser", "-u", "telegraf", "--", "cat", str(current / "credentials.env")], capture_output=True)
            assert forbidden.returncode != 0, "Telegraf can read credential file directly"
            run("runuser", "-u", "telegraf", "--", "test", "-r", str(current / "telegraf.conf"))
            assert run("systemctl", "show", "server-monitor.service", "--property=User", "--value").stdout.strip() == "telegraf"
            install("smoke-two", "smoke-token-two")
            second = os.readlink("/etc/server-monitor/current")
            assert os.readlink("/etc/server-monitor/previous") == first
            wait_for(lambda: any(s["server_id"] == "smoke-two" for s in samples))
            samples.clear()
            run("python3", str(project / "scripts/install.py"), "--rollback")
            assert os.readlink("/etc/server-monitor/current") == first
            assert os.readlink("/etc/server-monitor/previous") == second
            wait_for(lambda: any(s["server_id"] == "smoke-one" for s in samples))
            # A valid config with an executable that exits immediately must roll back.
            unit_template = project / "systemd/server-monitor.service"
            original_unit = unit_template.read_text()
            unit_template.write_text(original_unit.replace("ExecStart=/usr/bin/telegraf --config /etc/server-monitor/current/telegraf.conf", "ExecStart=/bin/false"))
            try:
                try:
                    install("smoke-two", "smoke-token-two")
                except subprocess.CalledProcessError:
                    pass
                else:
                    raise AssertionError("Immediately failing unit was accepted")
            finally:
                unit_template.write_text(original_unit)
            assert os.readlink("/etc/server-monitor/current") == first, "Failed startup did not restore live config"
            run("systemctl", "is-active", "--quiet", "server-monitor.service")
            invalid = subprocess.run(["python3", str(project / "scripts/install.py"), "--url", "http://example.com", "--server-id", "bad"], capture_output=True)
            assert invalid.returncode != 0 and os.readlink("/etc/server-monitor/current") == first
            if original_packaged is not None:
                assert packaged.read_bytes() == original_packaged, "Existing package config changed"
                assert subprocess.run(["systemctl", "is-enabled", "telegraf.service"], capture_output=True).stdout == packaged_state
            assert not errors, errors
            journal = run("journalctl", "-u", "server-monitor.service", "--no-pager").stdout
            assert "smoke-token-one" not in journal and "smoke-token-two" not in journal, "Credential appeared in logs"
            report = {"platform": "Ubuntu 24.04", "architecture": run("dpkg", "--print-architecture").stdout.strip(),
                      "real_install_and_service": "passed", "https_service_delivery": "passed", "unprivileged_permissions": "passed",
                      "separate_filesystem_and_filters": "passed", "reinstall_and_rollback": "passed", "packaged_config_preserved": "passed"}
            report["failed_startup_restoration"] = "passed"
            report["terminal_setup_and_controls"] = "passed"
            Path("/tmp/ubuntu-smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
        finally:
            subprocess.run(["systemctl", "stop", "server-monitor.service"], capture_output=True)
            server.shutdown()
            server.server_close()
            subprocess.run(["umount", str(data_mount)], capture_output=True)
            subprocess.run(["umount", str(ignored_mount)], capture_output=True)
            trust.unlink(missing_ok=True)
            run("update-ca-certificates")


if __name__ == "__main__":
    main()
