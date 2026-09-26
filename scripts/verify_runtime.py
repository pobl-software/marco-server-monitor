#!/usr/bin/env python3
"""Test a Telegraf binary against a temporary loopback HTTPS receiver."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time

SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).with_name("install.py"))
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
TOKEN = "runtime-test-token"


def sample_points(payload):
    """Compare individual values, since retries may regroup partial samples."""
    points = set()
    for sample in payload["samples"]:
        for section, values in sample["host"].items():
            rows = values if isinstance(values, list) else [values]
            for row in rows:
                resource = row.get("mount", row.get("interface", row.get("device", "")))
                for field, value in row.items():
                    points.add((payload["server_id"], sample["collected_at"], section, resource, field, json.dumps(value)))
        for row in sample.get("containers", []):
            for field, value in row.items():
                points.add((payload["server_id"], sample["collected_at"], "container", row["id"], field, json.dumps(value)))
    return points


def process_usage(pid):
    if not Path(f"/proc/{pid}/stat").exists():
        return None
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    cpu_seconds = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    status = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
    return {"cpu_seconds": cpu_seconds, "rss_kib": int(status["VmRSS"].split()[0]),
            "peak_rss_kib": int(status["VmHWM"].split()[0])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telegraf", default="telegraf", help="Path to a Telegraf binary; it is not installed by this script")
    parser.add_argument("--seconds", type=int, default=28, help="Test duration, at least 20 seconds")
    parser.add_argument("--report", type=Path, help="Optional non-secret JSON report")
    args = parser.parse_args()
    if args.seconds < 20:
        parser.error("--seconds must be at least 20")
    state = {"requests": [], "errors": []}

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_POST(self):
            try:
                assert self.path == "/metrics", "unexpected endpoint path"
                assert self.headers.get("Authorization") == f"Bearer {TOKEN}", "bearer authentication missing"
                assert self.headers.get("Content-Type") == "application/json", "unexpected content type"
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert payload["schema_version"] == 1, "unexpected schema version"
                assert payload["server_id"] == "runtime-test", "missing server identity"
                assert payload.get("hostname"), "missing hostname"
                assert isinstance(payload.get("samples"), list) and payload["samples"], "invalid batch"
                for sample in payload["samples"]:
                    assert type(sample["collected_at"]) is int, "timestamp is not Unix seconds"
                    assert abs(time.time() - sample["collected_at"]) < 120, "unexpected timestamp units"
                    assert sample["host"], "missing host measurements"
                code = 503 if len(state["requests"]) < 2 else 204
                state["requests"].append({"code": code, "payload": payload})
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception as error:
                state["errors"].append(str(error))
                self.send_error(400)

    with tempfile.TemporaryDirectory(prefix="server-monitor-runtime-") as temp:
        directory = Path(temp)
        cert, key = directory / "cert.pem", directory / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        settings = argparse.Namespace(url=f"https://127.0.0.1:{server.server_port}/metrics", server_id="runtime-test",
                                      interval="2s", flush_interval="4s", interfaces=["eth*", "en*"])
        config = installer.render_config(settings)
        path = directory / "telegraf.conf"
        path.write_text(config)
        env = dict(os.environ, SERVER_MONITOR_TOKEN=TOKEN)
        # First prove the self-signed endpoint is rejected without a trusted CA.
        untrusted = directory / "untrusted.conf"
        untrusted.write_text(installer.render_config(settings))
        log_path = directory / "telegraf.log"
        with log_path.open("w+") as log:
            rejected = subprocess.Popen([args.telegraf, "--config", str(untrusted)], env=env, stdout=log, stderr=log)
            try:
                time.sleep(6)
            finally:
                rejected.terminate()
                rejected.wait(timeout=20)
            log.seek(0)
            rejection_log = log.read()
            assert not state["requests"], "Untrusted TLS certificate was accepted"
            assert "certificate" in rejection_log.lower(), "No certificate-validation error observed"
            log.seek(0)
            log.truncate()
            process = subprocess.Popen([args.telegraf, "--config", str(path)], env=dict(env, SSL_CERT_FILE=str(cert)), stdout=log, stderr=log)
            started = time.monotonic()
            baseline = None
            latest = None
            try:
                time.sleep(5)
                baseline = process_usage(process.pid)
                measured_from = time.monotonic()
                while time.monotonic() - started < args.seconds:
                    assert process.poll() is None, "Telegraf exited early"
                    time.sleep(0.5)
                latest = process_usage(process.pid)
                measured_seconds = time.monotonic() - measured_from
            finally:
                process.terminate()
                process.wait(timeout=20)
                server.shutdown()
                server.server_close()
            log.seek(0)
            logs = log.read()
            if state["errors"]:
                raise AssertionError("; ".join(state["errors"]))
            assert len(state["requests"]) >= 4, "Insufficient batches observed"
            assert [r["code"] for r in state["requests"][:2]] == [503, 503], "Outage was not simulated"
            failed = set().union(*(sample_points(r["payload"]) for r in state["requests"][:2]))
            accepted_requests = [r for r in state["requests"] if r["code"] == 204]
            accepted_samples = [s for r in accepted_requests for s in r["payload"]["samples"]]
            accepted = set().union(*(sample_points(r["payload"]) for r in accepted_requests))
            assert failed <= accepted, "Failed batches were not retried after recovery"
            names = {name for sample in accepted_samples for name in sample["host"]}
            assert {"cpu", "memory", "swap", "network", "system"} <= names, f"Missing host sections: {names}"
            for sample in accepted_samples:
                host = sample["host"]
                if "cpu" in host:
                    assert "usage_percent" in host["cpu"], "CPU percentage missing"
                for row in host.get("network", []):
                    assert row["interface"].startswith(("eth", "en")), "Virtual/loopback interface leaked"
                for row in host.get("disks", []):
                    assert row["filesystem"] not in {"tmpfs", "overlay", "squashfs"}, "Pseudo-filesystem leaked"
                for row in host.get("disk_io", []):
                    assert not row["device"].startswith(("loop", "ram", "fd")), "Virtual device leaked"
            assert TOKEN not in logs, "Token appeared in Telegraf logs"
            report = {"telegraf_version": subprocess.check_output([args.telegraf, "--version"], text=True).strip(),
                      "platform": os.uname().sysname, "architecture": os.uname().machine,
                      "requests": len(state["requests"]), "measurements": sorted(names),
                      "tls_verification": "passed", "authentication_and_json": "passed", "retry_recovery": "passed",
                      "resource_scope": "Telegraf process only; excludes short-lived HTTPS sender",
                      "sample_interval_seconds": 2, "flush_interval_seconds": 4,
                      "resource_measurement_seconds": round(measured_seconds, 2)}
            if baseline and latest:
                report.update(rss_mib=round(latest["rss_kib"] / 1024, 2), peak_rss_mib=round(latest["peak_rss_kib"] / 1024, 2),
                              cpu_percent_one_core=round(100 * (latest["cpu_seconds"] - baseline["cpu_seconds"]) / measured_seconds, 3))
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
