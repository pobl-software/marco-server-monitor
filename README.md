# Marco Server Monitor

Host and optional Docker container monitoring for Ubuntu servers, with metrics delivered to a CRM or other HTTPS receiver.

Marco uses [Telegraf](https://github.com/influxdata/telegraf) to collect CPU, memory, disk and network metrics, and includes a terminal control panel for setup and day-to-day management. It runs on Ubuntu 24.04 on cloud servers or physical machines.

The project includes the monitoring agent, installer and management tools. A separate receiving endpoint handles metric ingestion and storage. The monitor needs no AWS APIs, cloud credentials, local database or inbound port. Docker socket access is needed only when you enable Docker monitoring, which is disabled by default.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Collected metrics](#collected-metrics)
- [Configuration](#configuration)
- [Managing the monitor](#managing-the-monitor)
- [CRM integration](#crm-integration)
- [Documentation](#documentation)
- [Development and contributions](#development-and-contributions)

## Features

- **Host metrics:** CPU, RAM, swap, filesystems, disk I/O, network traffic, load and uptime.
- **Optional Docker metrics:** per-container CPU, memory, network, block I/O, writable-layer/root-filesystem storage, lifecycle status/uptime and configured health checks; enable or disable in **Configure**.
- **Direct HTTPS delivery:** JSON batches authenticated with a separate bearer token for each server.
- **Terminal control panel:** guided setup, live service status, configuration, logs and service controls.
- **Background operation:** a dedicated systemd service that starts at boot and collects as the unprivileged `telegraf` user.
- **Configuration rollback:** restore the previous configuration and token.
- **Provider independence:** stable server IDs that do not depend on IP addresses or cloud accounts.

Resource metrics do not establish whether a website or database is healthy. Docker health status reflects the container's own configured health check. Delivery uses an in-memory buffer, so telemetry can be lost during extended outages or restarts.

## Requirements

| Requirement | Details |
| --- | --- |
| Operating system | Ubuntu 24.04 with a running systemd instance |
| Architecture | amd64 or arm64 |
| Permissions | `sudo` access for installation and management |
| Network | Internet access for installation; outbound HTTPS to the receiver |
| Download tool | `curl` for the one-line installer; Git is not required |
| Terminal | Interactive SSH session, at least 76 columns × 24 rows |
| Receiver | HTTPS POST endpoint with a valid certificate and per-server bearer tokens |

The download installer adds Python 3 and CA certificates if missing, then setup installs Telegraf from the official signed InfluxData APT repository. Installing from a local checkout requires Python 3 to be available already.

## Quick start

### 1. Prepare the receiver

Create an endpoint that accepts the [CRM ingestion contract](docs/crm-integration.md). Register the server with a stable ID, such as `production-01`, and issue a unique bearer token for it.

### 2. Install on the Ubuntu server

Run this in an interactive SSH terminal on the Ubuntu server:

```sh
curl -fsSL https://raw.githubusercontent.com/pobl-software/marco-server-monitor/main/install.sh | sudo bash
```

The command runs repository code as root; review the source before running it. See the [installation guide](docs/installation.md) for forks, optional version selection and local installation.

Guided setup collects the endpoint URL, server ID and bearer token. Select **Save and start monitor** to apply the settings and start collection. Token input is masked. Escape cancels without applying settings.

### 3. Confirm delivery

The control panel is available at any time:

```sh
sudo server-monitor
```

Check that the service is running, then confirm fresh samples at the receiver after the first reporting interval—60 seconds by default. A running service or a successful local collection check does not prove HTTPS delivery.

For installation from a copied checkout, run `sudo ./monitor.sh` from the project directory. A demo mode previews the interface on a development machine without installation:

```sh
./monitor.sh --demo
```

## Collected metrics

Each request includes a stable `server_id` and hostname once. The versioned CRM format groups retained measurements into timestamped `samples`, with host sections and optional container rows.

| Measurement | Data |
| --- | --- |
| `host.cpu` | CPU active, I/O wait and steal percentages |
| `host.memory` | Total and available RAM in bytes |
| `host.swap` | Total and used swap in bytes |
| `host.disks[]` | Capacity, available/used bytes, usage percentage and inode total/available per mount |
| `host.disk_io[]` | Read/write byte and operation counters, device busy time |
| `host.network[]` | Receive/send bytes, errors and drops per interface |
| `host.system` | Load averages, CPU count and uptime in seconds |

Temporary filesystems and Docker overlays are excluded; other mounted filesystems, including separate database volumes, are eligible. Loop, RAM and floppy block devices are excluded. Network collection defaults to `eth*` and `en*`, excluding loopback and protocol-wide `all` metrics. Check unusual or bonded interface names with `ip -brief link`.

Docker monitoring is **disabled by default**, including for existing host-only installations. In **Configure**, select **Docker monitoring**, press **Space** to enable or disable it, then choose **Save and start monitor**. When enabled, samples include `host.docker` engine counts and `containers[]` rows combining per-container resource/status/health data and selected Compose labels. The [Docker setup guide](docs/installation.md#docker-container-monitoring) describes socket access; the [CRM contract](docs/crm-integration.md#docker-container-fields) describes fields and identity.

Docker timestamps are rounded to the configured collection interval so health and uptime can merge with resource metrics when collection completes near the same interval boundary. Slow collection can still produce partial rows. Containers without a configured health check omit `health`. Storage fields are `storage_writable_layer_bytes` and `storage_rootfs_bytes`; they exclude volumes, bind mounts and logs. Storage collection requires Docker Engine 23.0+. Update strict receivers for the [storage extension](docs/crm-integration.md#version-and-rollout), then save the configuration from updated source to apply it to an installed monitor.

Disk and network counters are cumulative. See the [integration guide](docs/crm-integration.md#rates-derived-values-and-counter-resets) for rate calculations and reboot handling. Resource usage measurements and their limits are recorded in [verification results](docs/verification.md#recorded-results).

## Configuration

| Setting | Default |
| --- | --- |
| Collection interval | `10s` |
| Reporting interval | `60s` |
| Network interfaces | `eth*`, `en*` |
| Maximum input batch | 1,000 Telegraf measurements |
| In-memory buffer | 10,000 metrics |

A buffered metric is one internal Telegraf measurement for one resource at one timestamp. The sender groups these into CRM samples; a request may contain multiple timestamps or partial samples when Telegraf splits a batch. Buffered measurements are lost on restart; a full buffer overwrites the oldest ones.

Use **Configure** in the control panel to change the endpoint, server ID, token, intervals or interface filters. Leave the token blank to keep the existing one. Configuration fields are pre-filled with the installed settings.

Local checkouts also support command-line configuration:

```sh
sudo ./install.sh \
  --url https://crm.example.com/api/server-metrics \
  --server-id production-01 \
  --interval 10s \
  --flush-interval 30s \
  --interfaces 'bond0'
```

The installer prompts for the token with hidden input. Quote interface globs. Reporting must be at least as long as collection; both intervals support whole seconds, minutes or hours, up to 24 hours. CLI reinstallation resets unspecified settings to their defaults.

See [installation and configuration](docs/installation.md) for unattended setup with `--token-file` and source updates. Keep tokens out of shell commands, URLs and source control.

## Managing the monitor

Open `sudo server-monitor`, then use arrows/Tab and Enter, or these shortcuts:

| Key | Action |
| --- | --- |
| **S** / **X** / **R** | Start / stop / restart |
| **C** | Configure settings or rotate the token |
| **K** | Check configuration and local metric collection |
| **B** | Restore the previous configuration and token |
| **L** | View recent logs with current/previous tokens redacted |
| **A** | Update from `main` using saved settings and token |
| **U** | Uninstall after confirmation |
| **Q** | Close the interface; the service continues running |

Stopping the service does not disable startup at boot. **Save and start** and **Rollback** both start the service. For a copied checkout, use `sudo ./monitor.sh` to open the panel.

Common command-line checks:

```sh
sudo systemctl status server-monitor.service --no-pager
sudo journalctl -u server-monitor.service -n 50 --no-pager
sudo server-monitor --check
```

Update to the latest code on `main`, reinstall with the saved settings and token, and restart the monitor:

```sh
sudo server-monitor --update
```

The update preserves startup at boot and attempts to restore the previous installation if it fails. Older installations need the one-line installer run once to obtain this command; choose **Update** in the reopened panel.

The downloaded launcher also supports `--rollback` and `--uninstall`. For a copied checkout, use `sudo ./install.sh` with the same options. Uninstall removes the managed service, configuration, credentials and backups; Telegraf, its APT repository and downloaded source remain.

See [operation and troubleshooting](docs/operations.md) for update behavior, rollback limits, delivery failures and removal details.

## CRM integration

The monitor sends JSON over HTTPS with `Content-Type: application/json` and `Authorization: Bearer <token>`. The receiving endpoint must accept POST directly, without redirects, and validate that the token belongs to the supplied server ID.

Telegraf passes each internal JSON batch to a bundled Python sender. The sender converts it to **CRM schema v1**, removes redundant fields/tags and groups values by collection time. It verifies the certificate and hostname, rejects all redirects (including HTTPS-to-HTTP), and returns failures to Telegraf for buffered retry. Tokens stay out of command arguments and receiver response bodies are never logged. Each batch starts a short-lived Python process; no local listener is opened.

Receivers must persist the complete request before acknowledging it. Samples can be partial: merge fields by server ID, collection time, section and resource identity rather than replacing a stored sample. Retry ingestion must be idempotent, and freshness must follow collection time. Update the receiver for schema v1 before applying this sender; pre-v1 rollback releases still send the legacy format. See the [rollout guide](docs/crm-integration.md#version-and-rollout).

The [full ingestion contract](docs/crm-integration.md) and [example payload](examples/payload.json) define the requirements for receiver implementations. CRM integration and storage are handled separately from the monitoring agent.

## Documentation

| Guide | Contents |
| --- | --- |
| [Installation and configuration](docs/installation.md) | Download/local installation, forks, pinned versions, unattended setup and collection settings |
| [Operation and troubleshooting](docs/operations.md) | Terminal controls, checks, updates, rollback, outages and uninstall |
| [CRM integration](docs/crm-integration.md) | Authentication, JSON contract, metric units, deduplication and freshness |
| [Verification](docs/verification.md) | Recorded results, runtime resource usage and Ubuntu smoke tests |

## Development and contributions

Bug reports, documentation improvements and patches are welcome. Reports should include the Ubuntu version, architecture, Telegraf version and steps to reproduce, with tokens and credential contents removed.

From the project directory, run the local checks with Python 3.11+:

```sh
python3 -m unittest discover -s tests -v
python3 tests/terminal_smoke.py
bash -n install.sh monitor.sh tests/run-linux.sh
```

A portable runtime check requires an existing Telegraf binary, Python 3 and OpenSSL:

```sh
python3 scripts/verify_runtime.py --telegraf /usr/bin/telegraf
```

It uses temporary processes and a loopback HTTPS receiver, makes no production requests and installs no service. For full service, rollback and boot checks in a disposable Ubuntu container, follow the [verification guide](docs/verification.md#repeat-the-isolated-linux-checks).

Upstream documentation: [Telegraf installation](https://docs.influxdata.com/telegraf/v1/install/), [command output](https://github.com/influxdata/telegraf/tree/master/plugins/outputs/exec), [agent settings](https://docs.influxdata.com/telegraf/v1/configuration/agent/) and [JSON output](https://docs.influxdata.com/telegraf/v1/data_formats/output/json/).
