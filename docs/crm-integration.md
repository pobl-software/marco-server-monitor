# CRM ingestion contract

Create one HTTPS POST endpoint at the URL supplied during installation. No InfluxDB protocol is involved. The caller sends `Content-Type: application/json` and `Authorization: Bearer <per-server token>`; do not depend on cookies, sessions, redirects or AWS identity.

The bundled sender verifies the TLS certificate and hostname and makes exactly one POST per batch. All redirects are rejected, including redirects to another HTTPS endpoint or the same host over HTTP. Return a direct 2xx acknowledgement; a redirected login page must not count as successful delivery. Response bodies are neither read nor logged.

## Authenticate and save

1. Look up the bearer token using your normal secure credential handling. Associate it with one registered `server_id` and reject metrics claiming another ID. Do not trust `server_id` alone.
2. Accept a JSON object containing a nonempty `metrics` array. Each entry has `name`, `fields`, `tags` and `timestamp`; allow additional fields and measurement tags to avoid breaking on Telegraf upgrades. Fields may contain numbers, strings and booleans (Docker identity, health and lifecycle fields are not all numeric). Numeric counters can exceed 32-bit integers; preserve precision, including 64-bit byte counters.
3. Validate and persist the entire batch before returning an empty `204` or another successful 2xx response. For an endpoint that queues work, only acknowledge after durable enqueueing. Avoid partial-save ambiguity. Batches contain up to 1,000 entries with this configuration; set request limits accordingly, e.g. at least 2 MiB.
4. Deduplicate by `(server_id, name, canonical sorted tags, timestamp)`. Include the resource tags so different interfaces/filesystems at the same timestamp remain separate. Retried writes should be idempotent. If the same key has different fields, use an upsert policy consistently.

Examples of resource tags are `cpu=cpu-total`, `path`, `device`, `fstype`, `mode`, `name` (block device), and `interface`. Telegraf includes OS `host` plus the configured stable `server_id`. Treat measurement/resource names as data, not identifiers to interpolate into SQL.

See [representative payload](../examples/payload.json). Values are illustrative; a real minute-long batch usually contains several samples per resource. Metrics are timestamped at **collection**, not arrival. A batch is not an atomic whole-server snapshot, and can contain old and new samples together after an outage.

## Units and interpretation

| Name | Fields | Units / meaning |
| --- | --- | --- |
| `cpu` | `usage_active`, `usage_idle`, `usage_user`, `usage_system`, `usage_iowait`, `usage_steal` | Percent across all logical CPUs, 0–100 total CPU scale |
| `mem` | `total`, `available`, `used`, `cached`, `buffered` | Bytes |
| `mem` | `available_percent`, `used_percent` | Telegraf's memory percentages; prefer available RAM for pressure |
| `swap` | `total`, `free`, `used`; `used_percent` | Bytes; percentage |
| `disk` | `total`, `free`, `used`; `used_percent` | Bytes; percentage per filesystem, accounting for filesystem reservations |
| `disk` | `inodes_total`, `inodes_free`, `inodes_used`; `inodes_used_percent` | Counts; percentage |
| `diskio` | `reads`, `writes`, `read_bytes`, `write_bytes` | Cumulative completed operations and bytes per device |
| `diskio` | `read_time`, `write_time`, `io_time`, `weighted_io_time` | Cumulative milliseconds; concurrent operations can cause some time rates to exceed wall time |
| `diskio` | `iops_in_progress` | Current active requests, a gauge rather than a counter |
| `net` | `bytes_recv`, `bytes_sent`, `packets_recv`, `packets_sent`, `err_in`, `err_out`, `drop_in`, `drop_out` | Cumulative bytes, packets or counts per interface |
| `system` | `load1`, `load5`, `load15`, `n_cpus`, `uptime` | Load averages, logical CPU count, uptime seconds |

These counters generally accumulate since boot or device/interface creation. Compute disk/network rates separately for each resource; do not sum parent disk and partition counters or physical and bonded interfaces, which can double-count activity.

For a counter `C` at two sample timestamps `t` in seconds:

```text
bytes_per_second = (C_new - C_old) / (t_new - t_old)
Mbps = bytes_per_second * 8 / 1_000_000
IOPS = (operations_new - operations_old) / (t_new - t_old)
RAM pressure % = 100 * (1 - available / total)
```

Sort by collection time, not arrival order, and deduplicate before deriving rates. No rate is available for the first sample, nonpositive elapsed time, or a counter reset. Discard cross-reboot rate calculations: estimate the boot time from `system.timestamp - system.uptime`, allowing small timing jitter. A decreased uptime or a substantial boot-time shift indicates a new boot. Also start a new baseline if a device/interface counter decreases. Missing samples are unknown, not zero; a rate over a gap is an average over that gap. Do not use reported NIC speed as a cloud provider's guaranteed bandwidth ceiling.

RAM pressure uses available memory to account for reclaimable cache; `total - free` exaggerates usage. Load is not CPU percentage. Linux CPU active usage excludes idle and I/O wait; display I/O wait separately. Disk byte/inode percentages provide complementary exhaustion warnings.

## Docker container metrics

Docker monitoring is optional and disabled by default. When enabled, the existing JSON batches contain the following additional measurements. The sender, HTTPS endpoint and bearer-token authentication are unchanged. A receiver that allows only host measurement names or numeric fields must be extended before enabling this setting.

| Name | Representative fields | Meaning |
| --- | --- | --- |
| `docker` | `n_containers`, `n_containers_running`, `n_containers_stopped`, `n_containers_paused` | Engine-wide counts |
| `docker_container_cpu` | `usage_percent`, `usage_total`, throttling counters | Docker CPU percentage; accumulated CPU time in nanoseconds |
| `docker_container_mem` | `usage`, `limit`, `usage_percent` | Bytes and percentage; available fields depend on Docker/cgroup version |
| `docker_container_net` | `rx_bytes`, `tx_bytes`, packet/error/drop counters | Cumulative totals across container networks |
| `docker_container_blkio` | `io_service_bytes_recursive_read`, `io_service_bytes_recursive_write` | Cumulative read/write bytes, where supported |
| `docker_container_status` | `container_id`, `oomkilled`, `exitcode`, `pid`, `started_at`, `finished_at`, `uptime_ns` | Full container ID string, OOM boolean, lifecycle counts/times; start/finish Unix seconds and uptime nanoseconds |
| `docker_container_health` | `health_status`, `failing_streak` | Health string and failure count; present only with a configured Docker health check |

All measurements retain `server_id` and `host`. Container measurements add tags such as `container_name`, `container_image`, `container_version`, `container_status` and `source` (the first 12 characters of the container ID), plus selected Compose project/service labels. The full `container_id` is a string field on resource/status measurements, not necessarily a tag. Use `(server_id, source)` for container instance grouping and keep the existing deduplication key with all tags. Container names can be reused after recreation; start new counter baselines when the container identity changes or counters decrease. Selected Compose labels can group successive instances of one service.

Docker CPU percentage follows Docker's scale: 100% represents one CPU core, so a container using multiple cores can exceed 100%. It differs from the host `cpu` percentage, which is normalised across all logical CPUs. Container memory is a cgroup measurement and should not be interpreted as host available RAM. Network and block I/O rates use collection timestamps and counter deltas as above. Resource fields may be absent for stopped containers or unsupported cgroup counters; missing values are unknown, not zero. Removed containers stop producing measurements. Host and container totals overlap and should not be added together.

The default includes running, paused, restarting, exited, dead and created containers. Container logs, environment values, image/volume storage-size queries and Swarm service metrics are not collected. The [Docker payload example](../examples/docker-payload.json) shows mixed numeric/string/boolean fields; the [Telegraf plugin documentation](https://github.com/influxdata/telegraf/blob/master/plugins/inputs/docker/README.md) describes the full version-dependent field set.

## Availability and time

Store both receiver arrival time and collection time. For each server, update `last_fresh_sample_at` to the maximum accepted **collection timestamp**, never just `now()` when a batch arrives. Flag monitoring unavailable when the latest credible sample is over three minutes old. An old buffered batch must not clear that flag.

Ensure both servers and receiver have synchronised clocks. Future timestamps should not keep a server healthy indefinitely: reject/quarantine implausibly future samples (for example over 60 seconds ahead), and track clock problems separately. Loss of telemetry means monitoring unavailable; it does not distinguish an agent failure, network outage or server outage. Keep the CRM on separate infrastructure for useful outage detection.

The three-minute default assumes 60-second reporting. If you configure a longer reporting interval, increase the CRM's missing-sample threshold to at least three reporting intervals.

## Delivery limits

Telegraf retries failed HTTP writes at subsequent flushes; even permanent endpoint/auth errors can continue retrying. Use non-2xx statuses for rejected/unsaved batches and return a direct success for accepted batches. Do not rely on `Retry-After` or an application-specific response body being interpreted. Request timeouts may produce duplicate batches. The 10,000-metric buffer drops the oldest pending metrics when full and is lost on restart; the duration it covers depends on resource count. This project sends uncompressed JSON and opens no agent listener.

Telegraf's command output passes each JSON batch through stdin to the bundled HTTPS sender. A nonzero sender exit preserves Telegraf's retry behavior. The HTTPS request timeout is 10 seconds; Telegraf terminates a sender exceeding 12 seconds. Batches larger than 16 MiB are rejected locally; configure receiver body limits for the metrics your servers produce. Each send starts a short-lived Python process under the collector's user.
