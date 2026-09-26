# Installation and configuration

[Back to the README](../README.md) · [Operation and troubleshooting](operations.md)

## Contents

- [Download and guided setup](#download-and-guided-setup)
- [Downloaded source and updates](#downloaded-source-and-updates)
- [Install from a local checkout](#install-from-a-local-checkout)
- [Unattended installation](#unattended-installation)
- [Collection and reporting settings](#collection-and-reporting-settings)
- [Docker container monitoring](#docker-container-monitoring)

## Download and guided setup

Run this in an interactive SSH terminal on each Ubuntu 24.04 server:

```sh
curl -fsSL https://raw.githubusercontent.com/pobl-software/marco-server-monitor/main/install.sh | sudo bash
```

No commit SHA or checksum is required. The installer defaults to the latest source on `main`, downloads the complete source archive over verified HTTPS, installs Python 3/CA certificates if missing, and opens guided setup. It reconnects input to your SSH terminal so the form accepts keystrokes even though the script arrived through a pipe.

Ubuntu 24.04, amd64/arm64, curl, sudo, a running systemd instance and internet access are required. Noninteractive sessions and other operating systems are rejected. Use `ssh -t` when launching through an SSH command; do not install on your development Mac.

The command runs repository code as root, so use a repository you trust and review the source. The installer checks archive structure and source syntax before activation; downloads do not require checksum verification.

For a fork, download the script from that fork and pass `--repo OWNER/REPO` after `sudo bash -s --`. You can optionally select a branch, tag or commit with `--ref`, using the same version in the raw script URL. Git and GitHub credentials are not required on monitored servers. Archive links follow GitHub's [source archive format](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives).

## Downloaded source and updates

The downloaded source lives in root-owned `/opt/server-monitor`, separate from installed settings and credentials in `/etc/server-monitor`. A `/usr/local/bin/server-monitor` launcher opens the control panel:

```sh
sudo server-monitor
```

Run the one-line download command again to update the source and reopen setup. A complete bundle is checked before switching sources; the current and previous source versions are retained. A failed download or invalid bundle leaves the previous source active. Updating source does not automatically change installed settings, tokens or the running service; use **Configure** and **Save and start** in the UI to apply changes. Existing HTTP-output installations must be reconfigured to install the secure sender. These source versions are separate from the configuration rollback action.

## Install from a local checkout

Alternatively, copy this entire project to the server and use the local commands below. Local installation needs Python 3 already available.

Register each server in your CRM and issue a different bearer token for each. Choose a stable server ID independent of its IP address or provider. For guided setup, from the project directory on that server:

```sh
sudo ./monitor.sh
```

On first launch, the terminal interface asks for the CRM URL, server ID and masked bearer token. Collection/reporting intervals, network filters and the Docker monitoring toggle have defaults you can adjust in the same form. Docker monitoring starts disabled. Select **Save and start monitor** to install and activate it. Escape cancels without changing anything.

Running `sudo ./install.sh` without arguments opens the same interface. The existing command-line installer remains available for scripts or unattended use:

```sh
sudo ./install.sh \
  --url https://crm.example.com/api/server-metrics \
  --server-id production-01
```

The installer prompts for the token with hidden input. The endpoint must already accept the [documented JSON contract](crm-integration.md). Use a valid HTTPS certificate and an endpoint that accepts POST directly, without redirects. Don't put credentials in the URL.

The installer adds the official signed InfluxData APT repository if needed, installs the current stable Telegraf package, validates the configuration **as the Telegraf user without sending metrics**, then starts `server-monitor.service`. The packaged default `telegraf.service` is disabled only if this installer newly installs the package. An existing Telegraf package, service and `/etc/telegraf` configuration are preserved.

## Unattended installation

For unattended installation, put the token alone in an owner-only regular file (mode 600). Use `sudoedit /root/server-monitor.token` to enter it, then `sudo chmod 600 /root/server-monitor.token`. Do not put the token in a shell command, source control, or a command-line argument:

```sh
sudo ./install.sh \
  --url https://crm.example.com/api/server-metrics \
  --server-id production-02 \
  --token-file /root/server-monitor.token
```

Token files may end with one newline. Symlinks, group/world-readable files, multiline values and unsafe bearer-token characters are rejected. Treat both the source token file and installed credential files as secrets.

## Collection and reporting settings

Defaults are `10s` collection, `60s` reporting and interface filters `eth*` and `en*`. Use **Configure** in the terminal panel to edit the installed settings while preserving the other values.

Override settings by reinstalling with the complete desired options:

```sh
sudo ./install.sh \
  --url https://crm.example.com/api/server-metrics \
  --server-id production-01 \
  --interval 10s \
  --flush-interval 30s \
  --interfaces 'bond0'
```

Quote globs to keep your shell from expanding them. Whole-second/minute/hour intervals are supported (`10s`, `1m`, `1h`); reporting must be at least as long as collection and neither can exceed 24 hours. Unspecified settings return to their documented defaults on reinstall.

## Docker container monitoring

Open **Configure** and move to **Docker monitoring**. **Space** toggles **Enabled / Disabled**; Right/E enables and Left/D disables. Choose **Save and start monitor** to apply the change. Escape cancels. The dashboard shows the saved setting, and Configure preserves it when editing other values. New and existing host-only installations default to **Disabled**.

When enabled, Telegraf's [Docker input](https://github.com/influxdata/telegraf/blob/master/plugins/inputs/docker/README.md) collects container CPU, memory, total network and block I/O counters, lifecycle status and Docker health status when configured. It also collects engine container counts. Running, paused, restarting, exited, dead and created containers are included; resource fields depend on the container state and Docker/cgroup support. Only Compose project/service labels are included; logs and environment values are not sent. See the [receiver contract](crm-integration.md#docker-container-metrics).

Docker must already be installed and running at `/var/run/docker.sock`. This option supports the standard root-owned socket with read/write permission for a dedicated non-root group (normally `docker`); rootless Docker and custom endpoints are not configured by this toggle. Enabling does not install Docker, change socket permissions or add the Telegraf account to a system-wide group. The installer grants the socket's group only to `server-monitor.service` and its collection check, and orders startup after `docker.service`. [Docker socket access grants powerful host control](https://docs.docker.com/engine/install/linux-postinstall/#manage-docker-as-a-non-root-user); the configuration form displays this before saving.

Disabling removes the Docker input and the monitor-added supplementary group on restart. Existing account memberships configured outside this project are unchanged. Host collection continues with the usual settings. Missing sockets or failed collection checks leave the current configuration active. Rollback restores the Docker setting and its service permissions together; a changed socket group requires reconfiguration before restoring a Docker-enabled release.

For unattended installation, add `--docker` to the complete installer command. `--no-docker` explicitly disables it; omitting both uses the disabled default. CLI reinstallation resets unspecified settings to defaults, while the configuration form preserves saved values.
