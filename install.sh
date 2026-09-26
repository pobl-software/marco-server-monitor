#!/usr/bin/env bash
# Local installer and standalone GitHub bootstrap. No Git required.
set -euo pipefail

main() {
  local source_path="${BASH_SOURCE[0]:-}" project_dir=""
  if [[ -n "$source_path" && -f "$source_path" ]]; then
    project_dir="$(cd -- "$(dirname -- "$source_path")" && pwd)"
  fi
  # Download options explicitly request bootstrap mode, including in a checkout.
  local downloading=false argument
  for argument in "$@"; do
    case "$argument" in --repo|--ref) downloading=true ;; esac
  done
  if [[ -n "$project_dir" && -f "$project_dir/scripts/install.py" && -f "$project_dir/scripts/terminal_ui.py" && "$downloading" == false ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo 'Python 3 is required. On Ubuntu: sudo apt-get install python3' >&2
      exit 1
    fi
    if [[ $# -eq 0 ]]; then
      exec python3 "$project_dir/scripts/terminal_ui.py"
    fi
    exec python3 "$project_dir/scripts/install.py" "$@"
  fi

  local repository="pobl-software/marco-server-monitor" ref="main"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --repo|--ref)
        if [[ $# -lt 2 ]]; then echo "Missing value for $1." >&2; exit 1; fi
        case "$1" in
          --repo) repository="$2" ;;
          --ref) ref="$2" ;;
        esac
        shift 2 ;;
      -h|--help)
        echo 'Download and open Server Monitor setup on Ubuntu 24.04.'
        echo 'Usage: curl -fsSL https://raw.githubusercontent.com/pobl-software/marco-server-monitor/main/install.sh | sudo bash -s -- [--ref TAG_OR_COMMIT] [--repo OWNER/REPO]'
        exit 0 ;;
      *) echo "Unknown download option: $1" >&2; exit 1 ;;
    esac
  done
  if [[ ! "$repository" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$ || "$repository" == *..* ]]; then
    echo 'Supply your public GitHub repository as --repo OWNER/REPO.' >&2; exit 1
  fi
  if [[ ! "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ || "$ref" == *..* ]]; then
    echo 'Invalid GitHub branch, tag or commit.' >&2; exit 1
  fi
  if [[ $(id -u) != 0 ]]; then echo 'Run this command with sudo on the Ubuntu server.' >&2; exit 1; fi
  if [[ $(uname -s) != Linux || ! -f /etc/os-release ]]; then echo 'Requires Ubuntu 24.04; nothing was installed.' >&2; exit 1; fi
  # This is the operating system's own root-owned release file.
  . /etc/os-release
  if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then echo 'Requires Ubuntu 24.04; nothing was installed.' >&2; exit 1; fi
  if [[ ! -d /run/systemd/system ]]; then echo 'A running systemd instance is required.' >&2; exit 1; fi
  case "$(dpkg --print-architecture)" in amd64|arm64) ;; *) echo 'Supported architectures are amd64 and arm64.' >&2; exit 1 ;; esac
  if [[ ! -t 1 || "${TERM:-dumb}" == dumb || -z "${TERM:-}" ]] || ! ( : </dev/tty ) 2>/dev/null; then
    echo 'Guided setup needs an interactive terminal. Connect over SSH (ssh -t for remote commands).' >&2; exit 1
  fi
  command -v curl >/dev/null || { echo 'curl is required.' >&2; exit 1; }
  if ! command -v python3 >/dev/null || [[ ! -f /etc/ssl/certs/ca-certificates.crt ]]; then
    echo 'Installing Python 3 and CA certificates…'
    apt-get update </dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3 ca-certificates </dev/null
  fi
  local download_dir
  download_dir="$(mktemp -d)"
  trap 'rm -rf -- "$download_dir"' EXIT
  local encoded_ref
  encoded_ref="$(python3 -c 'import sys; from urllib.parse import quote; print(quote(sys.argv[1], safe=""))' "$ref")"
  echo "Downloading Server Monitor from $repository ($ref)…"
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 10 --max-time 120 --max-filesize 67108864 \
    "https://github.com/$repository/archive/$encoded_ref.tar.gz" --output "$download_dir/source.tar.gz"
  python3 - "$download_dir/source.tar.gz" <<'SERVER_MONITOR_BOOTSTRAP_PYTHON'
import ast
import fcntl
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid

MARKER = "Managed source bundle by server-monitor"
LAUNCHER_MARKER = "# Managed launcher by server-monitor"


def owned(path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError(f"Refusing an unsafe or unrelated path: {path}")


def managed(directory, launcher):
    if directory.exists() or directory.is_symlink():
        owned(directory, directory=True)
        owned(directory / ".source-managed")
        if (directory / ".source-managed").read_text() != MARKER + "\n":
            raise ValueError("Source directory is unrelated; it will not be replaced.")
    if launcher.exists() or launcher.is_symlink():
        owned(launcher)
        if not launcher.read_text().startswith("#!/usr/bin/env bash\n" + LAUNCHER_MARKER + "\n"):
            raise ValueError("An unrelated server-monitor command exists; it will not be replaced.")


def current_release(directory):
    link = directory / "current"
    if not link.exists() and not link.is_symlink():
        return None
    if not link.is_symlink() or link.lstat().st_uid != os.geteuid():
        raise ValueError("Invalid source release link.")
    target = os.readlink(link)
    if not re.fullmatch(r"releases/[0-9a-f]{32}", target):
        raise ValueError("Invalid source release link.")
    owned(directory / target, directory=True)
    owned(directory / target / ".source-release")
    if (directory / target / ".source-release").read_text() != MARKER + "\n":
        raise ValueError("Invalid source release marker.")
    return target


def extract(archive, destination):
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        if not members or len(members) > 20000 or sum(m.size for m in members) > 134217728:
            raise ValueError("Source archive is empty or too large.")
        root, seen = None, set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or "\\" in member.name:
                raise ValueError("Unsafe path in source archive.")
            if not (member.isdir() or member.isfile()) or member.name in seen:
                raise ValueError("Links, special files and duplicate paths are not allowed in source archives.")
            seen.add(member.name)
            if root is None:
                root = path.parts[0]
            if path.parts[0] != root:
                raise ValueError("Source archive must contain one project directory.")
            if len(path.parts) == 1:
                if not member.isdir():
                    raise ValueError("Invalid source archive root.")
                continue
            target = destination.joinpath(*path.parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with bundle.extractfile(member) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    for required in ("install.sh", "monitor.sh", "scripts/install.py", "scripts/terminal_ui.py",
                     "scripts/send_metrics.py", "config/telegraf.conf.tmpl", "systemd/server-monitor.service"):
        if not (destination / required).is_file():
            raise ValueError(f"Incomplete source archive: missing {required}.")
    for script in ("install.sh", "monitor.sh"):
        subprocess.run(["bash", "-n", str(destination / script)], check=True, stdin=subprocess.DEVNULL)
    for script in ("scripts/install.py", "scripts/terminal_ui.py", "scripts/send_metrics.py"):
        ast.parse((destination / script).read_text(), filename=script)


def install_bundle(archive, directory=Path("/opt/server-monitor"), launcher=Path("/usr/local/bin/server-monitor")):
    managed(directory, launcher)
    directory.mkdir(mode=0o755, exist_ok=True)
    (directory / ".source-managed").write_text(MARKER + "\n")
    lock = directory / ".download-lock"
    # Never follow a planted symlink for the lock or release directory.
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        owned(lock)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        releases = directory / "releases"
        if releases.exists() or releases.is_symlink():
            owned(releases, directory=True)
        releases.mkdir(mode=0o755, exist_ok=True)
        old = current_release(directory)
        target = "releases/" + uuid.uuid4().hex
        candidate = directory / target
        candidate.mkdir(mode=0o755)
        temporary_link = directory / (".current-" + uuid.uuid4().hex)
        try:
            extract(archive, candidate)
            (candidate / ".source-release").write_text(MARKER + "\n")
            launcher.parent.mkdir(parents=True, exist_ok=True)
            text = ("#!/usr/bin/env bash\n" + LAUNCHER_MARKER + "\nset -euo pipefail\n"
                    + 'exec bash "' + str(directory) + '/current/install.sh" "$@"\n')
            # Write the launcher atomically only after validating the complete bundle.
            with tempfile.NamedTemporaryFile(mode="w", dir=launcher.parent, prefix=".server-monitor-", delete=False) as output:
                name = Path(output.name)
                output.write(text)
            try:
                name.chmod(0o755)
                temporary_link.symlink_to(target)
                os.replace(temporary_link, directory / "current")
                try:
                    os.replace(name, launcher)
                except OSError:
                    if old:
                        temporary_link.symlink_to(old)
                        os.replace(temporary_link, directory / "current")
                    else:
                        (directory / "current").unlink()
                    raise
            finally:
                name.unlink(missing_ok=True)
        except BaseException:
            temporary_link.unlink(missing_ok=True)
            shutil.rmtree(candidate)
            raise
        # Keep the new source and the previous source; only prune our own marked releases.
        for entry in releases.iterdir():
            if entry.name in {Path(target).name, Path(old).name if old else ""}:
                continue
            if entry.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", entry.name):
                continue
            marker = entry / ".source-release"
            if marker.is_file() and not marker.is_symlink() and marker.read_text() == MARKER + "\n":
                shutil.rmtree(entry)
    return directory / "current/install.sh"


if __name__ == "__main__":
    try:
        install_bundle(Path(sys.argv[1]))
    except (OSError, ValueError, tarfile.TarError, SyntaxError, subprocess.CalledProcessError) as error:
        print(f"Download installation failed: {error}", file=sys.stderr)
        sys.exit(1)
SERVER_MONITOR_BOOTSTRAP_PYTHON
  rm -rf -- "$download_dir"
  trap - EXIT
  echo 'Download installed. Reopen the control panel later with: sudo server-monitor'
  echo 'Opening guided setup…'
  # curl occupied stdin. curses must read keystrokes from the SSH terminal instead.
  exec bash /opt/server-monitor/current/install.sh </dev/tty
}

# The shell parses the complete function before it can reconnect stdin to the TTY.
main "$@"
