#!/usr/bin/env python3
"""Piped downloader and real TTY checks, only in the disposable Ubuntu fixture.

The HTTP transport is replaced with a local archive: no public repository exists
until the owner publishes it. The rest of the bootstrap runs unmodified.
"""
import argparse
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from terminal_smoke import TerminalSession

PROJECT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--installed', action='store_true')
    args = parser.parse_args()
    if os.environ.get('SERVER_MONITOR_TEST_CONTAINER') != '1' or not Path('/.dockerenv').exists():
        raise SystemExit('Run only in the disposable test container.')
    with tempfile.TemporaryDirectory(prefix='bootstrap-fixture-') as name:
        fixture = Path(name)
        archive = fixture / 'source.tar.gz'
        with tarfile.open(archive, 'w:gz') as bundle:
            def clean(info):
                if any(part in {'.git', '__pycache__', 'test-results', '.DS_Store'} for part in Path(info.name).parts):
                    return None
                return info
            bundle.add(PROJECT, arcname='server-monitor-main', filter=clean)
        curl = fixture / 'curl'
        curl.write_text('''#!/usr/bin/python3
import os, shutil, sys
from pathlib import Path
if os.environ.get('BOOTSTRAP_FIXTURE_FAILURE') == '1':
    sys.exit(22)
if '--output' in sys.argv:
    url = next(value for value in sys.argv if value.startswith('https://'))
    target = Path(sys.argv[sys.argv.index('--output') + 1])
    assert url == 'https://github.com/pobl-software/marco-server-monitor/archive/main.tar.gz'
    shutil.copyfile(os.environ['BOOTSTRAP_FIXTURE_ARCHIVE'], target)
    if os.environ.get('BOOTSTRAP_FIXTURE_INVALID') == '1':
        target.write_bytes(b'invalid archive')
else:
    sys.stdout.buffer.write(Path('/project/install.sh').read_bytes())
''')
        curl.chmod(0o755)
        environment = {'PATH': str(fixture) + ':' + os.environ['PATH'], 'BOOTSTRAP_FIXTURE_ARCHIVE': str(archive)}
        command = ['bash', '-o', 'pipefail', '-c',
                   'curl -fsSL https://raw.githubusercontent.com/pobl-software/marco-server-monitor/main/install.sh | bash']
        sources = Path('/opt/server-monitor')
        credentials = Path('/etc/server-monitor/current/credentials.env')
        before = credentials.read_bytes() if args.installed else None
        for attempt in range(2):
            with TerminalSession(PROJECT, command=command, environment=environment, controlling_terminal=True) as session:
                session.wait_for('STOPPED' if args.installed else 'First setup')
                if not args.installed:
                    session.send('\x1b')
                    session.wait_for('service controls')
                session.quit()
            assert sources.joinpath('current/install.sh').is_file()
            assert Path('/usr/local/bin/server-monitor').is_file()
        assert len(list((sources / 'releases').iterdir())) == 2
        if before is not None:
            assert credentials.read_bytes() == before, 'Download replaced CRM credentials'
            subprocess.run(['/usr/local/bin/server-monitor', '--check'], check=True)
        else:
            assert not credentials.exists(), 'Cancelling setup installed credentials'
        with TerminalSession(PROJECT, command=['/usr/local/bin/server-monitor'], controlling_terminal=True) as session:
            session.wait_for('STOPPED' if args.installed else 'First setup')
            if not args.installed:
                session.send('\x1b')
                session.wait_for('service controls')
            session.quit()
        previous = (sources / 'current').readlink()
        failed_environment = dict(environment, BOOTSTRAP_FIXTURE_FAILURE='1')
        # Read the bootstrap from disk so only its archive download fails.
        bootstrap_command = ['bash', '/project/install.sh', '--repo', 'pobl-software/marco-server-monitor']
        with TerminalSession(PROJECT, command=bootstrap_command,
                             environment=failed_environment, controlling_terminal=True) as session:
            session.wait_for('Downloading Server Monitor')
            session.process.wait(timeout=10)
            assert session.process.returncode != 0
        assert (sources / 'current').readlink() == previous
        assert sources.joinpath('current/install.sh').is_file()
        invalid_environment = dict(environment, BOOTSTRAP_FIXTURE_INVALID='1')
        with TerminalSession(PROJECT, command=bootstrap_command, environment=invalid_environment, controlling_terminal=True) as session:
            session.wait_for('Download installation failed')
            session.process.wait(timeout=10)
            assert session.process.returncode != 0
        assert (sources / 'current').readlink() == previous
        if before is not None:
            assert credentials.read_bytes() == before
        print('Piped bootstrap: TTY setup, global command, updates and download/invalid-archive failure preservation passed.')


if __name__ == '__main__':
    main()
