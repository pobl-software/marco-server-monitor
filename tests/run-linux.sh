#!/usr/bin/env bash
# Disposable systemd container; no production endpoints or host directory mounts.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --bootstrap-only ) ]]; then
  echo 'Usage: bash tests/run-linux.sh [--bootstrap-only]' >&2
  exit 1
fi
bootstrap_only="${1:-}"
container_name="server-monitor-test-$$"
test_image="server-monitor-test"
docker buildx build --load -t "$test_image" -f tests/Dockerfile .
docker run -d --name "$container_name" --privileged --cgroupns private --tmpfs /run --tmpfs /run/lock "$test_image"
trap 'docker rm -f "$container_name" >/dev/null' EXIT
ready=false
for attempt in {1..30}; do
  if docker exec "$container_name" test -d /run/systemd/system; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo 'systemd did not start in the test container.' >&2
  exit 1
fi
docker exec "$container_name" python3 -m unittest discover -s tests -bv
docker exec "$container_name" python3 tests/terminal_smoke.py
docker exec "$container_name" python3 tests/bootstrap_smoke.py
if [[ "$bootstrap_only" == --bootstrap-only ]]; then
  echo 'Ubuntu unit tests and piped bootstrap checks passed.'
  exit 0
fi
docker exec "$container_name" python3 tests/ubuntu_smoke.py
docker exec "$container_name" python3 tests/bootstrap_smoke.py --installed
docker exec "$container_name" python3 scripts/verify_runtime.py --telegraf /usr/bin/telegraf --report /tmp/runtime-report.json
mkdir -p test-results
docker cp "$container_name:/tmp/runtime-report.json" test-results/runtime.json
docker cp "$container_name:/tmp/ubuntu-smoke-report.json" test-results/ubuntu-smoke.json
# systemd can clear /tmp during boot; copy reports before rebooting.
docker restart "$container_name"
ready=false
for attempt in {1..30}; do
  if docker exec "$container_name" systemctl is-active --quiet server-monitor.service; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo 'Monitor did not restart after container reboot.' >&2
  exit 1
fi
docker exec "$container_name" python3 tests/terminal_smoke.py --uninstall
echo 'Linux tests and automatic startup passed; reports are in test-results/.'
