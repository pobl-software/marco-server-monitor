# CRM ingestion contract

Marco sends a compact, versioned JSON payload over HTTPS. The sender converts Telegraf measurements into the CRM format before delivery; raw Telegraf measurement names, `fields` and `tags` are internal and are not part of this API.

See the [host payload](../examples/payload.json), [Docker payload](../examples/docker-payload.json) and [JSON Schema](payload.schema.json). Examples are illustrative. Actual requests contain all retained collection times in the batch, and may contain partial samples.

## Version and rollout

The envelope contains `schema_version: 1`, `server_id`, `hostname` and a nonempty `samples` array. The server ID and hostname appear once per request. Each sample contains `collected_at` (Unix seconds), a `host` object and, when container data is present, a `containers` array. Host sections and resource fields are omitted when not collected. Container-only samples have `host: {}`. Empty resource arrays and invented zero values are not sent.

This format replaces the previous `{"metrics": [...]}` contract. Update the CRM receiver to support schema v1 **before applying the new sender to servers**. This repository contains the monitor, not the CRM implementation. During rollout, accept both the legacy format and schema v1 if older agents remain active. Deploying updated source alone does not replace the running sender: use **Configure → Save and start monitor**, or reinstall with the complete desired options. Each installed release retains its sender, so rolling back to a pre-v1 release restores the legacy format as well as its settings and token. Older buffered records processed by the new sender are converted using the same allowlist.

Schema v1 deliberately selects dashboard and health fields. Unrecognised measurements and unselected diagnostic fields are discarded; a batch containing only discarded measurements is acknowledged locally without an HTTP request. Selected values must have valid types, finite nonnegative numbers, a valid identity and a nonnegative integer collection time. Invalid JSON, mixed server/hostname batches or conflicting values for the same field/resource/time cause a nonzero sender exit without exposing input values in logs. A duplicate record with identical values is harmless. Optional fields can extend v1 with a coordinated receiver update; changes to existing field meanings or types require a new schema version.

The Docker storage extension adds optional `storage_writable_layer_bytes` and `storage_rootfs_bytes` fields. Storage-only fragments have `id` and `name` but may omit `state`, because Docker's disk-usage API does not supply it. Update a strict receiver to accept the current [JSON Schema](payload.schema.json) before installing this release. Existing lifecycle/resource rows still supply `state`.

## Authentication and acknowledgement

Create one HTTPS POST endpoint at the configured URL. Requests use `Content-Type: application/json` and `Authorization: Bearer <per-server token>`. Do not depend on cookies, redirects, sessions or AWS identity. The sender verifies TLS certificate and hostname, rejects every redirect, and makes one POST per nonempty formatted batch. Response bodies are neither read nor logged.

1. Associate the bearer token with one registered server and verify the top-level `server_id`. Do not trust the claimed ID alone.
2. Validate `schema_version`, the envelope and all supplied sample/resource fields. Preserve 64-bit integer precision; bytes and counters are integers, while percentages and load values can be fractional. Container health/state/name values are strings and `oom_killed` is a boolean. Treat names as data, never SQL identifiers.
3. Validate and persist the entire request before returning an empty `204` or another direct 2xx response. If queueing, acknowledge only after durable enqueueing. Avoid partial-save ambiguity.
4. Merge and deduplicate individual fields as described below. Network timeouts and buffered retries can deliver the same values more than once.

Requests are compact JSON without compression. The sender caps both its raw input and formatted output at 16 MiB. Telegraf batches up to 1,000 input measurements; this is **not** a limit of 1,000 complete snapshots or containers. Configure receiver body limits for the resource count on your servers.

## Host fields

All numbers below are nonnegative. Missing sections or fields mean unknown/unavailable, not zero.

| Section | Fields | Units / meaning |
| --- | --- | --- |
| `host.cpu` | `usage_percent`, `iowait_percent`, `steal_percent` | Percent across all logical CPUs, on a 0–100 host scale; active usage excludes idle and I/O wait |
| `host.memory` | `total_bytes`, `available_bytes` | Bytes; available memory accounts for reclaimable cache |
| `host.swap` | `total_bytes`, `used_bytes` | Bytes |
| `host.system` | `cpu_count`, `uptime_seconds`, `load_1m`, `load_5m`, `load_15m` | Logical CPU count, uptime seconds and load averages |
| `host.disks[]` | `mount`; optional `device`, `filesystem`; `total_bytes`, `available_bytes`, `used_bytes`, `usage_percent`, `inodes_total`, `inodes_available` | Per mounted filesystem; identify each row by `mount` |
| `host.disk_io[]` | `device`, `read_bytes`, `written_bytes`, `read_operations`, `write_operations`, `busy_time_ms` | Cumulative device counters; busy time is milliseconds |
| `host.network[]` | `interface`, `received_bytes`, `sent_bytes`, `receive_errors`, `send_errors`, `receive_drops`, `send_drops` | Cumulative per-interface byte/error/drop counters |
| `host.docker` | `total_containers`, `running_containers`, `stopped_containers`, `paused_containers` | Engine counts, only when Docker collection supplies them |

Disk `usage_percent` is retained because filesystem reservations affect available space: `total_bytes - available_bytes` is not necessarily `used_bytes`. Use the reported percentage for disk exhaustion warnings. Inode exhaustion is separate from byte capacity.

The payload removes idle/user/system CPU splits, duplicate memory/swap percentages, cache/buffer details, packet counts, detailed I/O timing and internal resource tags. Rates are not calculated by the sender, and samples are not downsampled to the latest value. Every retained collection time remains available for graphs.

## Docker container fields

Docker collection is optional and **disabled by default**. Each `containers[]` row has `id` and `name`; lifecycle/resource rows also have `state`, which may be absent in a storage-only fragment. Other selected fields appear only when available. The ID is the 12-character hexadecimal Docker source ID; use `(server_id, id)` for grouping, not the reusable container name. The same ID is available even in health-only or storage-only partial batches, so resource and lifecycle measurements can be merged consistently. IDs are shortened Docker identifiers rather than globally unique identifiers; scope them to the server. Selected Compose labels appear as `compose_project` and `compose_service` when available and can group replacement instances of a service.

| Fields | Units / meaning |
| --- | --- |
| `cpu_percent` | Docker CPU scale: 100% is one core, so values may exceed 100%; differs from the host CPU scale |
| `memory_used_bytes`, `memory_limit_bytes` | Reported cgroup memory usage and limit, in bytes; neither is host available RAM |
| `network_received_bytes`, `network_sent_bytes` | Cumulative totals across container networks |
| `disk_read_bytes`, `disk_written_bytes` | Cumulative container block I/O bytes, where supported |
| `storage_writable_layer_bytes` | Current size of files created/changed in the container's writable layer, in bytes |
| `storage_rootfs_bytes` | Current root filesystem size including image layers, in bytes; shared layers appear in multiple containers |
| `health`, `health_failures` | Docker health string and consecutive failure count; only for configured health checks |
| `oom_killed`, `exit_code` | Boolean OOM flag and integer exit code |
| `started_at`, `finished_at`, `uptime_seconds` | Unix-second lifecycle timestamps and whole-second uptime; `0` can mean no recorded start/finish |
| `compose_project`, `compose_service` | Selected Compose project/service labels |

The Docker input uses `time_source = "collection_start"`, so CPU, memory, network, block I/O, status, health and storage measurements from one gather share its start time. This avoids separate rows caused by Docker stats arriving seconds after inspection. The sender combines measurements with the **same container ID and supplied collection second** into one row; it does not round arbitrary timestamps or carry health/uptime forward from earlier collections. Older configurations can still produce separate status and resource samples. It selects CPU/network/block I/O totals and excludes per-core/per-device duplicates. State and resource support determine which fields are present. Removed containers stop producing samples; an absent row does not prove removal or zero usage. Engine counts can include containers without resource data and must not be inferred from the size of a partial `containers` array.

Docker emits health only for a configured `HEALTHCHECK`. An absent `health` field does not mean healthy or unhealthy; check `state` separately. Uptime is reported when Docker supplies a recorded start time; for a stopped container it describes its last run, not ongoing uptime.

Storage sizes are gauges, not block I/O counters. Both exclude mounted volumes, bind mounts and Docker logs; a database storing data in a volume can have a small writable layer despite a large database. `storage_rootfs_bytes` already includes the writable layer and must not be added to it, or summed across containers to estimate host disk use because image layers may be shared. Unavailable sizes (Docker's `-1` sentinel) are omitted; a measured zero is retained. Container disk-usage collection requires Docker Engine 23.0+ (API 1.42+) and can increase collection cost on large filesystems. The input queries container storage only; image and volume disk-usage records are not sent.

Detailed cgroup counters, duplicate full container IDs, PID, CPU nanosecond counters, engine host/version, image metadata and unrelated labels are omitted. Logs and environment values are not collected or sent. Container resource metrics and Docker health checks do not establish application health unless the configured health check actually probes it. Host and container usage overlap and must not be added together.

## Partial samples, deduplication and retries

Collection timestamps belong to measurements, not HTTP requests. A flush can contain multiple collection times, old buffered samples and only some resources for a given time. Samples are grouped by the original Unix second, ordered chronologically, and resource arrays are ordered by their identifier. The format is deterministic for reordered input and repeated identical records, but **a sample is not an atomic server snapshot**.

Telegraf can split one collection across requests or regroup records during retry. Merge supplied fields for the same `(server_id, collected_at)` and resource identity; do not replace an existing sample or array with a partial fragment. For host scalar sections, identify values by section and field. For disks use `mount`; for disk I/O use `device`; for networks use `interface`; for containers use `id`. A suitable storage uniqueness key is `(server_id, collected_at, section, resource_id, field)` or an equivalent table-specific key. Upsert each supplied field idempotently. Preserve resource metadata once in resource tables if desired; receiving metadata does not require duplicating it in every history row.

A zero is a real observation, so preserve it and booleans such as `oom_killed: false`. Absence must not erase previously received fields at that same collection time. If overlapping fragments disagree, use an explicit receiver conflict policy rather than treating arrival order as collection order. A hostname is display metadata and must not change the server's registered identity.

## Rates, derived values and counter resets

For a cumulative counter `C` at collection times `t`, measured in seconds:

```text
bytes_per_second = (C_new - C_old) / (t_new - t_old)
Mbps = bytes_per_second * 8 / 1_000_000
IOPS = (operations_new - operations_old) / (t_new - t_old)
RAM pressure % = 100 * (1 - available_bytes / total_bytes)
swap usage % = 100 * used_bytes / total_bytes
inode usage % = 100 * (1 - inodes_available / inodes_total)
```

Guard zero denominators. Sort by collection time, merge fragments and deduplicate before deriving rates. Do not calculate a rate from the first sample, nonpositive elapsed time, a decreased counter or a changed resource identity. A rate over a missing-data gap is an average over the gap.

For hosts, estimate boot time from `collected_at - host.system.uptime_seconds`, allowing timing jitter. A decreased uptime or substantial boot-time change starts a new counter baseline. Containers can restart without changing ID: a changed nonzero `started_at`, decreased uptime or decreased counter also starts a new baseline. Recreated containers receive a new ID. Missing lifecycle data cannot prove continuity across an outage.

Do not sum parent disk/partition counters or physical/bonded interfaces, which can double-count activity. Load averages are not CPU percentages, and reported NIC speed is not a cloud provider's guaranteed bandwidth ceiling.

## Freshness and delivery limits

Store both receiver arrival time and `collected_at`. Update `last_fresh_sample_at` with the maximum credible collection time, never just the request arrival time. Old buffered samples must not clear an unavailable-monitor flag. Track host and container freshness separately; container-only data does not prove current host metrics are available.

With the default 60-second reporting interval, flag monitoring unavailable when fresh telemetry is over three minutes old. Increase this threshold to at least three reporting intervals when reporting is slower. Missing telemetry cannot distinguish an agent, network or server failure. Keep the CRM on separate infrastructure for useful outage detection.

Keep server and receiver clocks synchronised. Reject or quarantine implausibly future collection times (for example more than 60 seconds ahead); a future timestamp must not keep a server healthy indefinitely.

Telegraf retries failed writes on subsequent flushes. Non-2xx responses and formatting errors return a nonzero sender exit, preserving the buffer/retry behavior. The in-memory 10,000-measurement buffer drops the oldest pending records when full and is lost on restart. This is best-effort telemetry, not durable audit storage. The HTTPS timeout is 10 seconds and Telegraf terminates a sender exceeding 12 seconds. Each request starts a short-lived Python sender; no local agent listener is opened.
