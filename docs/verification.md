# Verification and Ubuntu smoke testing

## Recorded results

The Docker monitoring addition was checked locally on **2026-09-26**: **90 automated tests passed**, including disabled defaults, saved toggle values, service-scoped socket groups, failed enable/activation recovery, enable/disable rollback, and Docker JSON delivery with string/boolean fields to a loopback HTTPS receiver. The real pseudo-terminal preview passed enabling, saving, reopening and disabling Docker monitoring. Rendering also passed at the minimum 76 × 24 terminal size. Shell syntax and diff whitespace checks passed.

Live Docker collection and the Ubuntu systemd harness were **not rerun for this addition** because the local Docker engine was unavailable. Installer lifecycle tests use command fixtures; they do not establish live Docker API or systemd behavior. Before enabling on a server, run the collection check and confirm `docker_container_*` samples at the receiver. The results below describe the earlier host-only implementation.

Verified on **2026-09-26**, in an isolated **Ubuntu 24.04 arm64** systemd container running on the local OrbStack Linux VM, with **Telegraf 1.40.1**. No production server or CRM endpoint was used and Telegraf was not installed on macOS.

- **76 automated tests passed locally and on Ubuntu:** installer and terminal forms/controls, including archive safety checks, sender/rollback pairing, legacy rollback refusal, and uninstall checks.
- **Redirect security checks passed:** the actual Python sender rejected 301/302/303/307/308 responses pointing to both HTTP and HTTPS loopback targets without contacting any target. An untrusted certificate was rejected before credentials were sent; receiver error bodies containing a test token were not logged.
- **Real fresh installation passed** through the signed APT repository. The default package service was disabled; the dedicated service collected as `telegraf`.
- **Service HTTPS delivery passed:** expected bearer token, numeric JSON fields, Unix-second timestamps and server/host tags were received at a temporary local endpoint.
- **Credential permissions passed:** mode 600, readable by root/systemd and unreadable directly by the Telegraf user; the configuration remained readable by the collector.
- **Filtering passed:** a separately mounted ext4 filesystem was reported, temporary/overlay filesystems were excluded and only configured interfaces were reported.
- **Reinstall and rollback passed:** endpoint configuration and token remained paired; the packaged configuration/service state were preserved when Telegraf was already installed. A candidate unit using `/bin/false` triggered restoration of the previous running service.
- **Automatic startup passed** after restarting the disposable systemd container. This verifies boot enablement, not a physical/cloud-server power-cycle.
- **Terminal setup and controls passed:** a first installation was completed through the curses form, the token was not echoed, and keyboard Start/Stop actions changed the actual systemd service while the screen refreshed its status. Configuration cancellation and a clean terminal exit were also verified on macOS and Linux.
- **GUI uninstall passed:** default cancellation left the service running; explicit confirmation removed its service/configuration/credentials/backups and showed Not installed. The shared Telegraf package, configuration and repository remained intact. Repeating the single CLI uninstall command succeeded without changing other services.
- **One-line bootstrap passed:** the standalone installer ran through a pipe and reattached to a real SSH-style pseudo-terminal without any commit or checksum arguments. First setup accepted cancellation; the global launcher opened the UI; repeat downloads retained two source versions. Failed downloads and invalid archives preserved the current source. HTTP was replaced with local fixtures because the repository is not yet public; public raw/archive URLs remain to be smoke-tested after publication. Unit tests also cover unrelated paths, traversal, symlinks, special files and malformed bundles.
- **Portable runtime check passed:** an untrusted TLS certificate was rejected, the trusted endpoint received seven batches, two initial HTTP 503 responses were retried, and every failed sample was subsequently delivered. CPU, RAM, swap, filesystem, disk I/O, network and system measurements were observed. Tokens did not appear in agent logs.

The portable check sampled every **2 seconds** and flushed every **4 seconds** to exercise recovery quickly. Before the secure-sender change, five runs measured for approximately **23 seconds after a five-second warm-up** observed **84.75–147.54 MiB RSS** and **0.475–0.561% of one CPU core**. These are short-run measurements for the standard full-plugin package, **not guaranteed overhead**, a stress test, or a measurement of a full 10,000-metric backlog. The shipped defaults are 10s/60s; memory may remain similar even with slower collection. Telegraf's CPU cost was small here, but its memory footprint is not tiny. Measure it on your servers if RAM is tight.

The latest secure-sender runtime run recorded **147.16 MiB collector RSS** and **0.518% of one CPU core**, with seven delivered batches and complete retry recovery. Resource values sample the Telegraf process only and exclude the short-lived Python sender. They do not measure total service peak usage or sender CPU overhead.

The amd64 installation path is covered by installer tests; actual runtime verification was arm64. CPU steal, swap activity and real cloud-volume performance cannot be validated under representative production load in this container.

## Repeat the isolated Linux checks

Docker with a running Linux engine is required. The harness needs a privileged **disposable test container** to run systemd and create temporary filesystem mounts; it mounts no host project or production data directories. It downloads Ubuntu packages, installs a temporary trusted test certificate only inside the container and removes the test container on exit:

```sh
bash tests/run-linux.sh
```

It runs the installer/UI/source-bundle and loopback sender security tests, a piped-bootstrap pseudo-terminal test before and after setup, a real pseudo-terminal preview, first setup through the actual curses form, Start/Stop against systemd through keyboard actions, service/rollback/filesystem smoke tests, the portable HTTPS/retry/resource check, a container reboot, and GUI uninstall cancellation/removal. It verifies that shared Telegraf configuration/repository files remain intact and that the single uninstall command can be repeated. Non-secret reports are copied to `test-results/` (ignored by source control). Build image/cache artifacts remain available for subsequent tests. These fixtures are not production receivers.

To run only the Ubuntu unit tests and one-line download/TTY checks, without installing Telegraf or exercising service delivery:

```sh
bash tests/run-linux.sh --bootstrap-only
```

The portable terminal test can also be run independently on macOS or Linux without changing any real service:

```sh
python3 tests/terminal_smoke.py
```

## Smoke test on each Ubuntu server

After copying the project and installing with that server's real endpoint, ID and token:

```sh
sudo ./install.sh --check
sudo systemctl is-enabled server-monitor.service
sudo systemctl is-active server-monitor.service
sudo systemctl show server-monitor.service --property=User --property=MainPID
sudo stat -Lc '%a %U %G %n' /etc/server-monitor/current/credentials.env
sudo journalctl -u server-monitor.service -n 50 --no-pager
ip -brief link
findmnt --real
timedatectl status
```

Expected: enabled and active, user `telegraf`, credentials mode 600/root ownership, valid collection and synchronised time. In the CRM, confirm the correct ID, samples at the collection interval and new batches within about one reporting interval. Confirm every database filesystem and desired network interface appears.

To inspect current process CPU/RAM without starting another collector:

```sh
monitor_pid=$(sudo systemctl show server-monitor.service --property=MainPID --value)
ps -p "$monitor_pid" -o pid,etime,%cpu,rss,args
```

`rss` is in KiB; `%cpu` here is a process-lifetime average. Observe it under your usual workload and during receiver outages. Do not assume the development-container numbers apply to your server.

At a suitable maintenance time, reboot the server using your normal process. Verify the enabled service returns and fresh CRM samples resume. Check missing-telemetry handling by stopping the agent briefly and confirming the CRM flags monitoring unavailable after three minutes, then restart it. These production-server checks are deliberately manual; no remote connection or reboot is performed by this project.

For endpoint changes, run installation again with the complete desired options, confirm delivery, then optionally exercise `sudo ./install.sh --rollback`. Remember that rollback also restores the previous token, which must still be accepted by your CRM.
