"""Update managed source and monitoring together; no credentials enter argv or logs."""
import argparse
import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import tomllib
import types

from install import InstallError, PROJECT, SERVICE, validate_options, validate_token

DEFAULT_REPOSITORY = "pobl-software/marco-server-monitor"


def load_bootstrap():
    # Share the standalone downloader's archive/path checks and source transaction.
    text = (PROJECT / "install.sh").read_text()
    source = text.split("<<'SERVER_MONITOR_BOOTSTRAP_PYTHON'\n", 1)[1].split("\nSERVER_MONITOR_BOOTSTRAP_PYTHON", 1)[0]
    module = types.ModuleType("server_monitor_source_bundle")
    exec(compile(source, str(PROJECT / "install.sh") + "/bootstrap", "exec"), module.__dict__)
    return module


def saved_configuration(installer):
    target = installer.release("current")
    if target is None:
        raise InstallError("Set up the monitor before updating it.")
    try:
        path = installer.directory / target
        config = tomllib.loads((path / "telegraf.conf").read_text())
        if "exec" in config["outputs"]:
            command = config["outputs"]["exec"][0]["command"]
            url = command[command.index("--url") + 1]
        else:
            url = config["outputs"]["http"][0]["url"]
        settings = argparse.Namespace(url=url, server_id=config["global_tags"]["server_id"],
                                      interval=config["agent"]["interval"], flush_interval=config["agent"]["flush_interval"],
                                      interfaces=config["inputs"]["net"][0]["interfaces"],
                                      docker_enabled="docker" in config["inputs"])
        validate_options(settings)
        credential = (path / "credentials.env").read_text()
        if not credential.startswith("SERVER_MONITOR_TOKEN="):
            raise ValueError
        token = validate_token(credential.removeprefix("SERVER_MONITOR_TOKEN=").removesuffix("\n"))
        return settings, token
    except (OSError, KeyError, IndexError, TypeError, ValueError):
        raise InstallError("Cannot read saved settings and credentials; update was not started.") from None


def apply_candidate(installer, candidate, settings, token, enabled):
    # Load the downloaded installer's code so new templates and sender ship together.
    name = "server_monitor_candidate_installer"
    spec = importlib.util.spec_from_file_location(name, candidate / "scripts/install.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        updated = module.Installer(installer.root, installer.commands, installer.progress)
        updated.enable_at_boot = enabled
        updated.install(settings, token)
    finally:
        sys.modules.pop(name, None)


def update_monitor(installer):
    # Caller holds the installation lock for the entire stop/download/activation.
    settings, token = saved_configuration(installer)
    if not installer.unit.is_file():
        raise InstallError("Managed service unit is missing; reconfigure before updating.")
    bootstrap = load_bootstrap()
    directory = installer.root / "opt/server-monitor"
    launcher = installer.root / "usr/local/bin/server-monitor"
    try:
        bootstrap.managed(directory, launcher)
        old_source = bootstrap.current_release(directory) if directory.exists() else None
        repository = DEFAULT_REPOSITORY
        if old_source:
            metadata = directory / old_source / ".source-repository"
            if metadata.exists() or metadata.is_symlink():
                bootstrap.owned(metadata)
                repository = metadata.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository) or ".." in repository:
            raise ValueError
    except (OSError, ValueError):
        raise InstallError("Source installation or repository metadata is unsafe; update was not started.") from None

    active = installer.run("systemctl", "is-active", "--quiet", SERVICE, check=False).returncode == 0
    enabled = installer.run("systemctl", "is-enabled", "--quiet", SERVICE, check=False).returncode == 0
    stopped = False
    try:
        installer.message("Stopping the monitor for update…")
        # Mark before stopping so interruption during the command also attempts recovery.
        stopped = True
        installer.run("systemctl", "stop", SERVICE)
        state = installer.run("systemctl", "show", SERVICE, "--property=ActiveState", "--value").stdout.strip()
        if state not in {"inactive", "failed"}:
            raise InstallError("Monitor did not stop; update was not applied.")
        installer.message("Fetching the latest monitor source from main…")
        with tempfile.TemporaryDirectory(prefix="server-monitor-update-") as temp:
            archive = Path(temp) / "source.tar.gz"
            installer.run("curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https",
                          "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "120",
                          "--max-filesize", "67108864", "--output", str(archive),
                          f"https://github.com/{repository}/archive/main.tar.gz", timeout=150)
            installer.message("Validating and installing the update with saved settings…")
            bootstrap.install_bundle(archive, directory, launcher,
                                     activate=lambda candidate: apply_candidate(installer, candidate, settings, token, enabled),
                                     repository=repository)
    except BaseException as failure:
        if stopped and active:
            try:
                installer.message("Update failed; restarting the previous monitor…")
                installer.run("systemctl", "reset-failed", SERVICE, check=False)
                installer.run("systemctl", "start", SERVICE)
                installer.wait_active()
            except BaseException:
                raise InstallError("Update failed and the previous monitor could not be restarted. Check server-monitor.service.") from None
        if isinstance(failure, KeyboardInterrupt):
            raise
        # Neither archive contents, import errors nor command output enter logs.
        raise InstallError("Update failed; previous source/configuration retained. No diagnostic output was printed to protect credentials.") from None
    installer.message("Monitor updated from main and started. Settings and token were retained; confirm fresh CRM samples.")
