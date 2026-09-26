#!/usr/bin/env python3
"""Ubuntu installer; no third-party Python dependencies or shell evaluation."""
import argparse
import contextlib
import fcntl
import getpass
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit
import uuid

PROJECT = Path(__file__).resolve().parent.parent
MARKER = "Managed by server-monitor"
FINGERPRINT = "24C975CBA61A024EE1B631787C3D57159FC2F927"
SERVICE = "server-monitor.service"
BASE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}


class InstallError(Exception):
    pass


def duration(value):
    match = re.fullmatch(r"([1-9][0-9]*)(s|m|h)", value)
    if not match:
        raise InstallError("Intervals must be positive whole seconds, minutes or hours (10s, 1m, 1h).")
    seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600}[match[2]]
    if seconds > 86400:
        raise InstallError("Intervals must not exceed 24 hours.")
    return seconds


def validate_options(args):
    try:
        url = urlsplit(args.url)
        port = url.port
        hostname = url.hostname
    except ValueError:
        raise InstallError("Endpoint is not a valid HTTPS URL.") from None
    if (url.scheme != "https" or not hostname or url.username is not None
            or url.password is not None or url.fragment or port == 0
            or re.search(r"[\s\x00-\x1f\x7f]", args.url)
            or "$" in args.url):
        raise InstallError("Use an HTTPS URL without credentials, fragments, whitespace or dollar signs.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.server_id):
        raise InstallError("Server ID must be 1–128 letters, digits, dots, underscores or hyphens, starting with a letter/digit.")
    if duration(args.flush_interval) < duration(args.interval):
        raise InstallError("Reporting interval must be at least the collection interval.")
    if not args.interfaces or any(not re.fullmatch(r"[A-Za-z0-9_*?\[\].:-]{1,64}", x) for x in args.interfaces):
        raise InstallError("Interface filters must be nonempty interface names or glob patterns.")
    if not isinstance(getattr(args, "docker_enabled", False), bool):
        raise InstallError("Docker monitoring must be enabled or disabled.")


def validate_token(token):
    # RFC 6750 b64token: deliberately excludes quotes, newlines, $ and backslashes.
    if not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,}", token) or len(token) > 8192:
        raise InstallError("Token must be a nonempty bearer token (letters, digits, . _ ~ + / - and trailing =; max 8192 characters).")
    return token


def read_token(path):
    if path is None:
        if not sys.stdin.isatty():
            raise InstallError("Without a terminal, supply --token-file with a protected file.")
        return validate_token(getpass.getpass("Per-server bearer token (hidden): "))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            allowed_owners = {os.getuid(), 0}
            if os.environ.get("SUDO_UID", "").isdigit():
                allowed_owners.add(int(os.environ["SUDO_UID"]))
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in allowed_owners:
                raise InstallError("Token file must be a regular non-symlink file, owned by you/root, with no group/other permissions (chmod 600).")
            token = stream.read(8194)
    except (OSError, UnicodeError):
        raise InstallError("Cannot securely read the token file; check ownership, permissions and path.") from None
    return validate_token(token.removesuffix("\n"))


def render_config(args, sender=None):
    text = (PROJECT / "config/telegraf.conf.tmpl").read_text()
    docker_input = '''[[inputs.docker]]
  endpoint = "unix:///var/run/docker.sock"
  timeout = "5s"
  # Round Docker's inspect/stats timestamps to the configured collection interval.
  precision = @@INTERVAL@@
  source_tag = true
  storage_objects = ["container"]
  container_state_include = ["running", "paused", "restarting", "exited", "dead", "created"]
  perdevice_include = []
  total_include = ["cpu", "blkio", "network"]
  docker_label_include = ["com.docker.compose.project", "com.docker.compose.service"]
  tag_env = []
  fieldinclude = ["n_containers", "n_containers_running", "n_containers_stopped", "n_containers_paused", "usage_percent", "usage", "limit", "rx_bytes", "tx_bytes", "io_service_bytes_recursive_read", "io_service_bytes_recursive_write", "oomkilled", "exitcode", "started_at", "finished_at", "uptime_ns", "health_status", "failing_streak", "size_rw", "size_root_fs"]
''' if getattr(args, "docker_enabled", False) else ""
    text = text.replace("@@DOCKER_INPUT@@", docker_input)
    replacements = {
        "SERVER_ID": args.server_id, "URL": args.url,
        "INTERVAL": args.interval, "FLUSH_INTERVAL": args.flush_interval,
        "INTERFACES": args.interfaces,
        "SENDER": str(sender or PROJECT / "scripts/send_metrics.py"),
    }
    for key, value in replacements.items():
        text = text.replace(f"@@{key}@@", json.dumps(value, ensure_ascii=True))
    return text


def render_unit(docker_groups=()):
    text = (PROJECT / "systemd/server-monitor.service").read_text()
    if docker_groups:
        text = text.replace("[Service]\n", "[Service]\nSupplementaryGroups=" + " ".join(map(str, docker_groups)) + "\n")
        text = text.replace("After=network-online.target\n", "After=network-online.target docker.service\n")
    return text


class Commands:
    def run(self, argv, *, check=True, env=None, user=None, group=None, extra_groups=(), timeout=180):
        options = {"env": dict(BASE_ENV, **(env or {})), "text": True,
                   "capture_output": True, "timeout": timeout}
        if user is not None:
            options.update(user=user, group=group, extra_groups=list(extra_groups), umask=0o077)
        try:
            result = subprocess.run(argv, **options)
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError(f"Could not complete {Path(argv[0]).name}; check dependencies or connectivity.") from None
        if check and result.returncode:
            # Never print a command's output: config errors can echo expanded secrets.
            raise InstallError(f"{Path(argv[0]).name} failed (exit {result.returncode}); no command output was printed to protect credentials.")
        return result


def atomic_write(path, content, mode, gid=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".server-monitor-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, gid)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_link(path, target):
    temp = path.parent / f".link-{uuid.uuid4().hex}"
    try:
        temp.symlink_to(target)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Installer:
    def __init__(self, root=Path("/"), commands=None, progress=None):
        self.root = root
        self.commands = commands or Commands()
        self.directory = root / "etc/server-monitor"
        self.unit = root / "etc/systemd/system" / SERVICE
        self.gid = 0
        self.uid = 0
        self.progress = progress

    def message(self, text):
        if self.progress:
            self.progress(text)
        else:
            print(text, flush=True)

    def run(self, *argv, **kwargs):
        return self.commands.run(list(argv), **kwargs)

    def preflight(self):
        if os.geteuid() != 0:
            raise InstallError("Run the installer with sudo on the Ubuntu server.")
        release = self.root / "etc/os-release"
        if not release.is_file():
            raise InstallError("This installer requires Ubuntu 24.04.")
        values = dict(re.findall(r'^([A-Z_]+)=[\"\']?([^\"\'\n]+)', release.read_text(), re.M))
        if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "24.04":
            raise InstallError("This installer supports Ubuntu 24.04; it does not install on macOS.")
        if not (self.root / "run/systemd/system").is_dir():
            raise InstallError("A running systemd instance is required; ordinary Docker containers are unsupported for installation.")
        arch = self.run("dpkg", "--print-architecture").stdout.strip()
        if arch not in {"amd64", "arm64"}:
            raise InstallError("Supported architectures are amd64 and arm64.")
        if self.directory.is_symlink() or self.unit.is_symlink():
            raise InstallError("Refusing symlinked installation directories or service units.")
        if self.directory.exists():
            marker = self.directory / ".managed"
            if not marker.is_file() or marker.read_text().strip() != MARKER:
                raise InstallError("/etc/server-monitor already exists and is not managed by this installer.")
            if (self.directory / "releases").is_symlink():
                raise InstallError("Refusing a symlinked releases directory.")
        if self.unit.exists() and not self.unit.read_text().startswith(f"# {MARKER}\n"):
            raise InstallError("An unrelated server-monitor.service already exists; it will not be overwritten.")
        for name in ("current", "previous"):
            self.release(name)

    def release(self, name):
        path = self.directory / name
        if not path.exists() and not path.is_symlink():
            return None
        if not path.is_symlink():
            raise InstallError(f"Expected a managed {name} release link.")
        target = os.readlink(path)
        if not re.fullmatch(r"releases/[0-9a-f]{32}", target):
            raise InstallError("Unexpected release link; refusing to modify the installation.")
        resolved = self.directory / target
        if resolved.is_symlink() or not resolved.is_dir():
            raise InstallError("Managed release is missing or is a symlink.")
        return target

    def prepare_package(self):
        result = self.run("dpkg-query", "-W", "-f=${Status}", "telegraf", check=False)
        if result.returncode == 0 and result.stdout.strip() == "install ok installed":
            if not (self.root / "usr/bin/telegraf").is_file():
                raise InstallError("Installed Telegraf package is missing /usr/bin/telegraf.")
            return
        if (self.root / "usr/bin/telegraf").exists():
            raise InstallError("An unmanaged /usr/bin/telegraf exists; refusing to replace it.")
        self.message("Installing Telegraf from the signed InfluxData APT repository…")
        self.run("apt-get", "update", timeout=600)
        self.run("apt-get", "install", "-y", "ca-certificates", "curl", "gnupg", env={"DEBIAN_FRONTEND": "noninteractive"}, timeout=600)
        sources = list((self.root / "etc/apt/sources.list.d").glob("*.list"))
        sources += list((self.root / "etc/apt/sources.list.d").glob("*.sources"))
        sources += [self.root / "etc/apt/sources.list"]
        existing_repository = any(p.exists() and "repos.influxdata.com" in p.read_text() for p in sources)
        if not existing_repository:
            with tempfile.TemporaryDirectory(prefix="server-monitor-key-") as temp:
                key = Path(temp) / "archive.key"
                self.run("curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https", "--max-time", "60", "--output", str(key), "https://repos.influxdata.com/influxdata-archive.key")
                details = self.run("gpg", "--batch", "--no-options", "--homedir", temp, "--show-keys", "--with-colons", str(key)).stdout
                fingerprints = [row.split(":")[9] for row in details.splitlines() if row.startswith("fpr:")]
                if not fingerprints or fingerprints[0] != FINGERPRINT:
                    raise InstallError("InfluxData signing-key fingerprint differs from the documented key; repository was not added.")
                dearmored = Path(temp) / "archive.gpg"
                self.run("gpg", "--batch", "--no-options", "--homedir", temp, "--dearmor", "--output", str(dearmored), str(key))
                keyring = self.root / "etc/apt/keyrings/server-monitor-influxdata.gpg"
                keyring.parent.mkdir(parents=True, exist_ok=True)
                if keyring.is_symlink() or (keyring.exists() and keyring.read_bytes() != dearmored.read_bytes()):
                    raise InstallError("An unrelated installer repository key already exists; it will not be replaced.")
                shutil.copyfile(dearmored, keyring)
                keyring.chmod(0o644)
                source = self.root / "etc/apt/sources.list.d/server-monitor-influxdata.list"
                if source.exists():
                    raise InstallError("Installer repository source already exists; inspect it before retrying.")
                atomic_write(source, "deb [signed-by=/etc/apt/keyrings/server-monitor-influxdata.gpg] https://repos.influxdata.com/debian stable main\n", 0o644)
        self.run("apt-get", "update", timeout=600)
        try:
            self.run("apt-get", "install", "-y", "telegraf", env={"DEBIAN_FRONTEND": "noninteractive"}, timeout=600)
        finally:
            installed = self.run("dpkg-query", "-W", "-f=${Status}", "telegraf", check=False)
            if installed.returncode == 0 and installed.stdout.strip() == "install ok installed":
                # Only this newly installed package's default service is changed.
                self.run("systemctl", "disable", "--now", "telegraf.service")

    def account(self):
        try:
            user = pwd.getpwnam("telegraf")
        except KeyError:
            raise InstallError("The Telegraf package must provide the telegraf account.") from None
        if user.pw_uid == 0:
            raise InstallError("Refusing to run metrics collection as root.")
        self.uid, self.gid = user.pw_uid, user.pw_gid

    def docker_groups(self, enabled):
        if not enabled:
            return []
        try:
            info = (self.root / "var/run/docker.sock").stat()
        except OSError:
            raise InstallError("Docker monitoring requires Docker running at /var/run/docker.sock. Start Docker or disable Docker monitoring.") from None
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_gid == 0
                or info.st_mode & 0o060 != 0o060):
            raise InstallError("Docker monitoring requires a root-owned Docker socket with read/write access for a dedicated non-root group.")
        return [info.st_gid]

    def validate_release(self, release):
        path = self.directory / release
        token = (path / "credentials.env").read_text().removeprefix("SERVER_MONITOR_TOKEN=").removesuffix("\n")
        validate_token(token)
        try:
            config = tomllib.loads((path / "telegraf.conf").read_text())
            if set(config["outputs"]) != {"exec"} or len(config["outputs"]["exec"]) != 1:
                raise ValueError
            command = config["outputs"]["exec"][0]["command"]
            sender = path / "send_metrics.py"
            if command != ["/usr/bin/python3", str(sender), "--url", command[-1]] or not sender.is_file():
                raise ValueError
            args = argparse.Namespace(url=command[-1], server_id=config["global_tags"]["server_id"],
                                      interval=config["agent"]["interval"], flush_interval=config["agent"]["flush_interval"],
                                      interfaces=config["inputs"]["net"][0]["interfaces"],
                                      docker_enabled="docker" in config["inputs"])
            validate_options(args)
        except (KeyError, IndexError, TypeError, ValueError):
            raise InstallError("Configuration lacks the secure HTTPS sender. Reconfigure to upgrade; legacy HTTP configurations cannot be rolled back to.") from None
        groups = self.docker_groups(args.docker_enabled)
        if groups:
            unit = (path / "server-monitor.service").read_text()
            if re.findall(r"^SupplementaryGroups=(.*)$", unit, re.M) != [str(groups[0])]:
                raise InstallError("Docker socket ownership changed. Reconfigure Docker monitoring before checking or restoring this configuration.")
        self.run("/usr/bin/python3", str(sender), "--url", args.url, "--check",
                 env={"SERVER_MONITOR_TOKEN": token}, user=self.uid, group=self.gid, timeout=15)
        self.run(str(self.root / "usr/bin/telegraf"), "--config", str(path / "telegraf.conf"), "--test",
                 env={"SERVER_MONITOR_TOKEN": token}, user=self.uid, group=self.gid, extra_groups=groups, timeout=60)

    def wait_active(self):
        for _ in range(5):
            time.sleep(1)
            if self.run("systemctl", "is-active", "--quiet", SERVICE, check=False).returncode != 0:
                raise InstallError("Monitoring service failed its startup check.")

    def activate(self, target):
        atomic_link(self.directory / "current", target)
        atomic_write(self.unit, (self.directory / target / "server-monitor.service").read_text(), 0o644)
        self.run("systemctl", "daemon-reload")
        self.run("systemctl", "enable" if getattr(self, "enable_at_boot", True) else "disable", SERVICE)
        self.run("systemctl", "restart", SERVICE)
        self.wait_active()

    def cleanup(self):
        keep = {self.release("current"), self.release("previous")}
        for path in (self.directory / "releases").iterdir():
            if (re.fullmatch(r"[0-9a-f]{32}", path.name) and not path.is_symlink()
                    and f"releases/{path.name}" not in keep):
                shutil.rmtree(path)

    def install(self, args, token):
        groups = self.docker_groups(getattr(args, "docker_enabled", False))
        self.prepare_package()
        self.account()
        self.directory.mkdir(exist_ok=True, mode=0o750)
        os.chown(self.directory, 0, self.gid)
        self.directory.chmod(0o750)
        atomic_write(self.directory / ".managed", MARKER + "\n", 0o640, self.gid)
        releases = self.directory / "releases"
        releases.mkdir(exist_ok=True, mode=0o750)
        os.chown(releases, 0, self.gid)
        releases.chmod(0o750)
        target = "releases/" + uuid.uuid4().hex
        candidate = self.directory / target
        candidate.mkdir(mode=0o750)
        os.chown(candidate, 0, self.gid)
        old = self.release("current")
        old_unit = self.unit.read_text() if self.unit.exists() else None
        was_active = self.run("systemctl", "is-active", "--quiet", SERVICE, check=False).returncode == 0
        was_enabled = self.run("systemctl", "is-enabled", "--quiet", SERVICE, check=False).returncode == 0
        switched = False
        try:
            atomic_write(candidate / "send_metrics.py", (PROJECT / "scripts/send_metrics.py").read_text(), 0o640, self.gid)
            atomic_write(candidate / "telegraf.conf", render_config(args, sender=candidate / "send_metrics.py"), 0o640, self.gid)
            atomic_write(candidate / "credentials.env", f"SERVER_MONITOR_TOKEN={token}\n", 0o600)
            atomic_write(candidate / "server-monitor.service", render_unit(groups), 0o640, self.gid)
            self.message("Validating configuration as the unprivileged Telegraf account (no metrics sent)…")
            self.validate_release(target)
            self.run("systemd-analyze", "verify", str(candidate / "server-monitor.service"))
            switched = True
            self.activate(target)
            if old:
                atomic_link(self.directory / "previous", old)
        except BaseException:
            if switched:
                self.restore_state(old, old_unit, was_active, was_enabled)
            shutil.rmtree(candidate)
            raise
        try:
            self.cleanup()
        except OSError:
            self.message("Monitor installed; some older release files could not be cleaned up.")
        self.message("Installed server-monitor.service. A running service does not confirm CRM delivery.")
        self.message("Check logs: sudo journalctl -u server-monitor.service -n 50 --no-pager")
        self.message("Confirm fresh samples in your CRM after the first reporting interval.")

    def restore_state(self, target, unit, active, enabled):
        self.run("systemctl", "stop", SERVICE, check=False)
        if target:
            atomic_link(self.directory / "current", target)
        else:
            (self.directory / "current").unlink(missing_ok=True)
        if unit is not None:
            atomic_write(self.unit, unit, 0o644)
        else:
            self.unit.unlink(missing_ok=True)
        self.run("systemctl", "daemon-reload")
        self.run("systemctl", "enable" if enabled else "disable", SERVICE, check=False)
        if active:
            self.run("systemctl", "reset-failed", SERVICE, check=False)
            self.run("systemctl", "start", SERVICE)

    def check(self):
        self.account()
        target = self.release("current")
        if not target:
            raise InstallError("No installed monitoring configuration found.")
        self.validate_release(target)
        self.message("Configuration and input collection passed; --check does not send metrics or test the receiver.")

    def update(self):
        from update import update_monitor
        update_monitor(self)

    def rollback(self):
        self.account()
        target, old = self.release("previous"), self.release("current")
        if not target or not old:
            raise InstallError("No previous configuration is available.")
        self.validate_release(target)
        unit = self.unit.read_text() if self.unit.exists() else None
        active = self.run("systemctl", "is-active", "--quiet", SERVICE, check=False).returncode == 0
        enabled = self.run("systemctl", "is-enabled", "--quiet", SERVICE, check=False).returncode == 0
        try:
            self.activate(target)
        except BaseException:
            self.restore_state(old, unit, active, enabled)
            raise
        atomic_link(self.directory / "previous", old)
        self.message("Restored the previous configuration and token; service restarted.")

    def uninstall(self):
        # Verify ownership before stopping anything or removing files. This also
        # allows cleanup of a managed first installation with no active release.
        self.preflight()
        if not self.directory.exists() and not self.unit.exists():
            self.message("Monitor is already uninstalled. Telegraf and its repository are unchanged.")
            return
        if self.unit.exists():
            self.message("Stopping the monitor and disabling startup at boot…")
            self.run("systemctl", "disable", "--now", SERVICE)
            state = self.run("systemctl", "show", SERVICE, "--property=ActiveState", "--value").stdout.strip()
            if state not in {"inactive", "failed"}:
                raise InstallError("Monitor did not stop; its configuration and credentials were not removed.")
            self.run("systemctl", "reset-failed", SERVICE, check=False)
        self.message("Removing the managed configuration, credentials and backups…")
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.unit.unlink(missing_ok=True)
        self.run("systemctl", "daemon-reload")
        self.message("Monitor uninstalled. Telegraf, its repository and other services are unchanged.")
        self.message("Revoke the per-server token in your CRM and delete any source token file separately.")


@contextlib.contextmanager
def installation_lock():
    fd = os.open("/run/lock/server-monitor-install.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("Another server-monitor installation is running.") from None
        yield
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description="Install host and optional Docker monitoring on Ubuntu 24.04.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Validate installed config/inputs without sending metrics")
    mode.add_argument("--rollback", action="store_true", help="Restore previous successful configuration and token")
    mode.add_argument("--uninstall", action="store_true", help="Stop and remove this monitor, retaining the Telegraf package")
    mode.add_argument("--update", action="store_true", help="Fetch main and reinstall/start the monitor with its saved settings and token")
    parser.add_argument("--url", help="CRM HTTPS ingestion URL")
    parser.add_argument("--server-id", help="Stable CRM server identifier")
    parser.add_argument("--token-file", type=Path, help="Owner-only token file; otherwise prompted with hidden input")
    parser.add_argument("--interval", default="10s", help="Collection interval (default: 10s)")
    parser.add_argument("--flush-interval", default="60s", help="Reporting interval (default: 60s)")
    parser.add_argument("--interfaces", nargs="+", default=["eth*", "en*"], help="Quoted interface names/globs (default: 'eth*' 'en*')")
    parser.add_argument("--docker", dest="docker_enabled", action=argparse.BooleanOptionalAction, default=False,
                        help="Enable Docker container metrics and service-only Docker socket access (default: disabled)")
    args = parser.parse_args()
    try:
        if not (args.check or args.rollback or args.uninstall or args.update):
            if not args.url or not args.server_id:
                parser.error("installation requires --url and --server-id")
            validate_options(args)
        installer = Installer()
        installer.preflight()
        with installation_lock():
            if args.check:
                installer.check()
            elif args.rollback:
                installer.rollback()
            elif args.uninstall:
                installer.uninstall()
            elif args.update:
                installer.update()
            else:
                token = read_token(args.token_file)
                installer.install(args, token)
    except (InstallError, OSError) as error:
        # Filesystem errors contain paths, not file contents. Command errors are sanitized.
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Installation interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
