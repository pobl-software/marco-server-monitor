#!/usr/bin/env python3
"""Format one Telegraf batch as CRM schema v1 and send it over verified HTTPS."""
import argparse
import http.client
import json
import math
import os
import re
import ssl
import sys
from urllib.parse import urlsplit

MAX_BATCH_BYTES = 16 * 1024 * 1024


class DeliveryError(Exception):
    """Messages are deliberately independent of credentials and receiver content."""


# Explicit mappings keep upstream additions out of the public CRM contract.
HOST_FIELDS = {
    "cpu": ("cpu", {"usage_active": "usage_percent", "usage_iowait": "iowait_percent", "usage_steal": "steal_percent"}),
    "mem": ("memory", {"total": "total_bytes", "available": "available_bytes"}),
    "swap": ("swap", {"total": "total_bytes", "used": "used_bytes"}),
    "system": ("system", {"n_cpus": "cpu_count", "uptime": "uptime_seconds", "load1": "load_1m", "load5": "load_5m", "load15": "load_15m"}),
    "docker": ("docker", {"n_containers": "total_containers", "n_containers_running": "running_containers",
                           "n_containers_stopped": "stopped_containers", "n_containers_paused": "paused_containers"}),
}
RESOURCE_FIELDS = {
    "disk": ("disks", "path", "mount", {"total": "total_bytes", "free": "available_bytes", "used": "used_bytes",
                                         "used_percent": "usage_percent", "inodes_total": "inodes_total", "inodes_free": "inodes_available"}),
    "diskio": ("disk_io", "name", "device", {"read_bytes": "read_bytes", "write_bytes": "written_bytes",
                                             "reads": "read_operations", "writes": "write_operations", "io_time": "busy_time_ms"}),
    "net": ("network", "interface", "interface", {"bytes_recv": "received_bytes", "bytes_sent": "sent_bytes",
                                                   "err_in": "receive_errors", "err_out": "send_errors",
                                                   "drop_in": "receive_drops", "drop_out": "send_drops"}),
}
CONTAINER_FIELDS = {
    "docker_container_cpu": {"usage_percent": "cpu_percent"},
    "docker_container_mem": {"usage": "memory_used_bytes", "limit": "memory_limit_bytes"},
    "docker_container_net": {"rx_bytes": "network_received_bytes", "tx_bytes": "network_sent_bytes"},
    "docker_container_blkio": {"io_service_bytes_recursive_read": "disk_read_bytes", "io_service_bytes_recursive_write": "disk_written_bytes"},
    "docker_container_status": {"oomkilled": "oom_killed", "exitcode": "exit_code", "started_at": "started_at", "finished_at": "finished_at"},
    "docker_container_health": {"health_status": "health", "failing_streak": "health_failures"},
}
FRACTIONAL_FIELDS = {"usage_active", "usage_iowait", "usage_steal", "usage_percent", "used_percent", "load1", "load5", "load15"}


def merge(target, values):
    for key, value in values.items():
        if key in target and target[key] != value:
            raise DeliveryError("Metric batch contains conflicting values for one resource and collection time.")
        target[key] = value


def selected_fields(fields, mapping):
    values = {}
    for source, destination in mapping.items():
        if source not in fields:
            continue
        value = fields[source]
        if source == "health_status":
            valid = isinstance(value, str) and bool(value)
        elif source == "oomkilled":
            valid = isinstance(value, bool)
        else:
            numeric_type = (int, float) if source in FRACTIONAL_FIELDS else int
            valid = (isinstance(value, numeric_type) and not isinstance(value, bool)
                     and value >= 0 and (not isinstance(value, float) or math.isfinite(value)))
        if not valid:
            raise DeliveryError("Metric batch contains an invalid field value.")
        if source in FRACTIONAL_FIELDS:
            try:
                value = float(value)
            except OverflowError:
                raise DeliveryError("Metric batch contains an invalid field value.") from None
            if not math.isfinite(value):
                raise DeliveryError("Metric batch contains an invalid field value.")
        values[destination] = value
    return values


def resource_tag(tags, key):
    value = tags.get(key)
    if not isinstance(value, str) or not value:
        raise DeliveryError("Metric batch is missing a resource identifier.")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DeliveryError("Metric batch contains duplicate JSON keys.")
        result[key] = value
    return result


def invalid_constant(unused):
    raise ValueError("Non-finite JSON numbers are unsupported.")


def format_payload(payload):
    """Stateless, deterministic conversion; keep collection times and partial samples."""
    if not payload or len(payload) > MAX_BATCH_BYTES:
        raise DeliveryError("Metric batch is empty or exceeds the 16 MiB delivery limit.")
    try:
        raw = json.loads(payload, object_pairs_hook=unique_object,
                         parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise DeliveryError("Metric batch is not valid JSON.") from None
    metrics = raw.get("metrics") if isinstance(raw, dict) else None
    if not isinstance(metrics, list) or not metrics:
        raise DeliveryError("Metric batch must contain a nonempty metrics array.")
    identity = None
    samples = {}
    resources = {}
    containers = {}
    for metric in metrics:
        if not isinstance(metric, dict):
            raise DeliveryError("Metric batch contains an invalid measurement.")
        name, tags, fields, timestamp = (metric.get(key) for key in ("name", "tags", "fields", "timestamp"))
        if (not isinstance(name, str) or not isinstance(tags, dict) or not isinstance(fields, dict)
                or type(timestamp) is not int or timestamp < 0):
            raise DeliveryError("Metric batch contains an invalid measurement or collection timestamp.")
        server_id, hostname = resource_tag(tags, "server_id"), resource_tag(tags, "host")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", server_id):
            raise DeliveryError("Metric batch contains an invalid server identifier.")
        if identity is not None and identity != (server_id, hostname):
            raise DeliveryError("Metric batch must contain exactly one server and hostname.")
        identity = (server_id, hostname)
        if name not in HOST_FIELDS and name not in RESOURCE_FIELDS and name not in CONTAINER_FIELDS:
            continue
        sample = samples.setdefault(timestamp, {"collected_at": timestamp, "host": {}})
        if name in HOST_FIELDS:
            if name == "cpu" and tags.get("cpu") != "cpu-total":
                continue
            section, mapping = HOST_FIELDS[name]
            values = selected_fields(fields, mapping)
            if values:
                merge(sample["host"].setdefault(section, {}), values)
        elif name in RESOURCE_FIELDS:
            section, tag, identifier, mapping = RESOURCE_FIELDS[name]
            values = selected_fields(fields, mapping)
            if not values:
                continue
            key = resource_tag(tags, tag)
            rows = resources.setdefault((timestamp, section), {})
            row = rows.setdefault(key, {identifier: key})
            merge(row, values)
            if name == "disk":
                for source, destination in (("device", "device"), ("fstype", "filesystem")):
                    if source in tags:
                        merge(row, {destination: resource_tag(tags, source)})
        else:
            if name == "docker_container_cpu" and tags.get("cpu") != "cpu-total":
                continue
            if name == "docker_container_net" and tags.get("network") != "total":
                continue
            if name == "docker_container_blkio" and tags.get("device") != "total":
                continue
            values = selected_fields(fields, CONTAINER_FIELDS[name])
            if name == "docker_container_status":
                # Telegraf reports started_at/finished_at as Unix nanoseconds; CRM wants seconds.
                for key in ("started_at", "finished_at"):
                    if key in values and values[key] >= 100_000_000_000_000_000:
                        values[key] = values[key] // 1_000_000_000
                if "uptime_ns" in fields:
                    uptime = selected_fields(fields, {"uptime_ns": "uptime_ns"})["uptime_ns"]
                    values["uptime_seconds"] = uptime // 1_000_000_000
            if not values:
                continue
            # The source tag is present even on health-only batches, unlike the full ID field.
            identifier = resource_tag(tags, "source")
            if not re.fullmatch(r"[0-9a-f]{12}", identifier):
                raise DeliveryError("Metric batch contains an invalid Docker container identifier.")
            row = containers.setdefault(timestamp, {}).setdefault(identifier, {"id": identifier})
            merge(row, {"name": resource_tag(tags, "container_name"), "state": resource_tag(tags, "container_status")})
            merge(row, values)
            for source, destination in (("com.docker.compose.project", "compose_project"), ("com.docker.compose.service", "compose_service")):
                if source in tags:
                    merge(row, {destination: resource_tag(tags, source)})
    for (timestamp, section), rows in resources.items():
        samples[timestamp]["host"][section] = [rows[key] for key in sorted(rows)]
    for timestamp, rows in containers.items():
        samples[timestamp]["containers"] = [rows[key] for key in sorted(rows)]
    result = {"schema_version": 1, "server_id": identity[0], "hostname": identity[1],
              "samples": [samples[key] for key in sorted(samples) if samples[key]["host"] or samples[key].get("containers")]}
    if not result["samples"]:
        return None  # An intentionally excluded measurement does not need delivery or retry.
    encoded = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    if len(encoded) > MAX_BATCH_BYTES:
        raise DeliveryError("Formatted metric batch exceeds the 16 MiB delivery limit.")
    return encoded


def endpoint(value):
    try:
        parts = urlsplit(value)
        port = parts.port
        if (parts.scheme != "https" or not parts.hostname or port == 0
                or parts.username is not None or parts.password is not None
                or parts.fragment or re.search(r"[\s\x00-\x1f\x7f]", value) or "$" in value):
            raise ValueError
        return parts.hostname, port, parts.path or "/", parts.query
    except ValueError:
        raise DeliveryError("Metric delivery requires a valid HTTPS endpoint without embedded credentials.") from None


def credential():
    token = os.environ.get("SERVER_MONITOR_TOKEN", "")
    if len(token) > 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,}", token):
        raise DeliveryError("Metric delivery requires a valid bearer token in the environment.")
    return token


def deliver(url, token, payload):
    host, port, path, query = endpoint(url)
    if not payload or len(payload) > MAX_BATCH_BYTES:
        raise DeliveryError("Metric batch is empty or exceeds the 16 MiB delivery limit.")
    connection = http.client.HTTPSConnection(host, port, timeout=10, context=ssl.create_default_context())
    try:
        # HTTPSConnection makes exactly one request. It never handles Location
        # headers, so neither credentials nor telemetry can follow a redirect.
        connection.request("POST", path + ("?" + query if query else ""), body=payload,
                           headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            detail = " (redirects are disabled)" if 300 <= response.status < 400 else ""
            raise DeliveryError(f"Metric delivery rejected: HTTP {response.status}{detail}.")
        # Only the status matters. Never read or log the receiver's body or URL.
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--check", action="store_true", help="Validate settings without sending metrics")
    args = parser.parse_args()
    try:
        endpoint(args.url)
        token = credential()
        if args.check:
            ssl.create_default_context()
            return 0
        payload = format_payload(sys.stdin.buffer.read(MAX_BATCH_BYTES + 1))
        if payload is not None:
            deliver(args.url, token, payload)
    except DeliveryError as error:
        print(str(error), file=sys.stderr)
        return 1
    except ssl.SSLCertVerificationError:
        print("Metric delivery failed: TLS certificate verification failed.", file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException):
        # Telegraf may put stderr in the journal. Do not print exceptions, which
        # can contain URLs, header values or arbitrary remote response data.
        print("Metric delivery failed: HTTPS connection or response error.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
