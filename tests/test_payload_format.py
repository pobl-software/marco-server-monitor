"""Verify the public CRM contract independently of HTTPS transport."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("payload_sender", PROJECT / "scripts/send_metrics.py")
sender = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sender)


def fixture(name):
    return json.loads((PROJECT / "tests/fixtures" / name).read_text())


def convert(metrics):
    result = sender.format_payload(json.dumps({"metrics": metrics}).encode())
    return json.loads(result) if result is not None else None


class PayloadTests(unittest.TestCase):
    def test_documented_examples_match_the_public_contract(self):
        for fixture_name, example_name in (("telegraf-host.json", "payload.json"), ("telegraf-docker.json", "docker-payload.json")):
            with self.subTest(example=example_name):
                self.assertEqual(convert(fixture(fixture_name)["metrics"]), json.loads((PROJECT / "examples" / example_name).read_text()))

    def test_host_contract_groups_measurements_and_keeps_essential_fields(self):
        payload = convert(fixture("telegraf-host.json")["metrics"])
        self.assertEqual(set(payload), {"schema_version", "server_id", "hostname", "samples"})
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["server_id"], "production-01")
        self.assertEqual(payload["hostname"], "ubuntu-01")
        self.assertEqual(len(payload["samples"]), 1)
        sample = payload["samples"][0]
        self.assertEqual(sample["collected_at"], 1790413200)
        self.assertNotIn("containers", sample)
        self.assertEqual(sample["host"], {
            "cpu": {"usage_percent": 23.0, "iowait_percent": 2.0, "steal_percent": 0.0},
            "memory": {"total_bytes": 8589934592, "available_bytes": 5368709120},
            "swap": {"total_bytes": 2147483648, "used_bytes": 1073741824},
            "system": {"cpu_count": 2, "uptime_seconds": 86400, "load_1m": 0.7, "load_5m": 0.5, "load_15m": 0.4},
            "disks": [{"mount": "/var/lib/docker", "device": "vdb", "filesystem": "ext4", "total_bytes": 107374182400,
                       "available_bytes": 64424509440, "used_bytes": 42949672960, "usage_percent": 40.0,
                       "inodes_total": 6553600, "inodes_available": 5898240}],
            "disk_io": [{"device": "vdb", "read_bytes": 12345678900, "written_bytes": 23456789000,
                         "read_operations": 1000, "write_operations": 2000, "busy_time_ms": 12000}],
            "network": [{"interface": "eth0", "received_bytes": 1234567890, "sent_bytes": 987654321,
                         "receive_errors": 0, "send_errors": 0, "receive_drops": 0, "send_drops": 0}],
        })

    def test_docker_contract_merges_resources_status_health_and_compose_identity(self):
        payload = convert(fixture("telegraf-docker.json")["metrics"])
        self.assertEqual(len(payload["samples"]), 1)
        sample = payload["samples"][0]
        self.assertEqual(sample["host"]["docker"], {"total_containers": 1, "running_containers": 1,
                                                   "stopped_containers": 0, "paused_containers": 0})
        self.assertEqual(sample["containers"], [{
            "id": "0123456789ab", "name": "crm-web-1", "state": "running", "health": "healthy", "health_failures": 0,
            "cpu_percent": 125.5, "memory_used_bytes": 268435456, "memory_limit_bytes": 1073741824,
            "network_received_bytes": 123456789, "network_sent_bytes": 987654321,
            "disk_read_bytes": 3456789, "disk_written_bytes": 4567890,
            "oom_killed": False, "exit_code": 0, "started_at": 1790423400, "finished_at": 0, "uptime_seconds": 600,
            "compose_project": "crm", "compose_service": "web",
        }])

    def test_reordered_batches_and_duplicate_records_produce_identical_bytes(self):
        metrics = fixture("telegraf-host.json")["metrics"] + fixture("telegraf-docker.json")["metrics"]
        first = sender.format_payload(json.dumps({"metrics": metrics}).encode())
        second = sender.format_payload(json.dumps({"metrics": list(reversed(metrics)) + [metrics[0]]}).encode())
        self.assertEqual(first, second)
        self.assertEqual([s["collected_at"] for s in json.loads(first)["samples"]], [1790413200, 1790424000])
        self.assertLess(len(first), len(json.dumps({"metrics": metrics}, separators=(",", ":")).encode()))

    def test_same_name_containers_with_different_ids_do_not_merge(self):
        first = fixture("telegraf-docker.json")["metrics"][0]
        second = copy.deepcopy(first)
        second["tags"]["source"] = "abcdef012345"
        second["fields"]["usage_percent"] = 50
        containers = convert([second, first])["samples"][0]["containers"]
        self.assertEqual([c["id"] for c in containers], ["0123456789ab", "abcdef012345"])
        self.assertEqual([c["cpu_percent"] for c in containers], [125.5, 50])

    def test_multiple_network_interfaces_and_mounts_remain_separate(self):
        metrics = fixture("telegraf-host.json")["metrics"]
        network = copy.deepcopy(next(m for m in metrics if m["name"] == "net"))
        network["tags"]["interface"] = "eth1"
        disk = copy.deepcopy(next(m for m in metrics if m["name"] == "disk"))
        disk["tags"]["path"] = "/data"
        host = convert([network, disk] + metrics)["samples"][0]["host"]
        self.assertEqual([r["interface"] for r in host["network"]], ["eth0", "eth1"])
        self.assertEqual([r["mount"] for r in host["disks"]], ["/data", "/var/lib/docker"])

    def test_partial_batches_preserve_identity_without_fabricating_metrics(self):
        metrics = fixture("telegraf-docker.json")["metrics"]
        health = next(m for m in metrics if m["name"] == "docker_container_health")
        status = next(m for m in metrics if m["name"] == "docker_container_status")
        for metric in (health, status):
            sample = convert([metric])["samples"][0]
            self.assertEqual(sample["host"], {})
            row = sample["containers"][0]
            self.assertEqual(row["id"], "0123456789ab")
            self.assertNotIn("cpu_percent", row)
            self.assertNotIn("memory_used_bytes", row)

    def test_collection_times_are_never_replaced_by_send_time_or_merged(self):
        first = fixture("telegraf-host.json")["metrics"][0]
        second = copy.deepcopy(first)
        second["timestamp"] += 10
        second["fields"]["usage_active"] = 42
        samples = convert([second, first])["samples"]
        self.assertEqual([s["collected_at"] for s in samples], [1790413200, 1790413210])
        self.assertEqual([s["host"]["cpu"]["usage_percent"] for s in samples], [23, 42])

    def test_large_integer_counters_and_zero_values_are_preserved(self):
        network = next(m for m in fixture("telegraf-host.json")["metrics"] if m["name"] == "net")
        network["fields"]["bytes_recv"] = 2 ** 64 - 1
        row = convert([network])["samples"][0]["host"]["network"][0]
        self.assertEqual(row["received_bytes"], 2 ** 64 - 1)
        self.assertIs(type(row["received_bytes"]), int)
        self.assertEqual(row["receive_errors"], 0)

    def test_diagnostics_unknown_measurements_and_per_device_docker_rows_are_ignored(self):
        metrics = fixture("telegraf-docker.json")["metrics"]
        expected = convert(metrics)
        unknown = copy.deepcopy(metrics[0])
        unknown["name"] = "unsupported_plugin"
        unknown["fields"] = {"secret": "not-for-the-crm"}
        per_cpu = copy.deepcopy(metrics[0])
        per_cpu["tags"]["cpu"] = "cpu0"
        per_net = copy.deepcopy(next(m for m in metrics if m["name"] == "docker_container_net"))
        per_net["tags"]["network"] = "eth0"
        per_io = copy.deepcopy(next(m for m in metrics if m["name"] == "docker_container_blkio"))
        per_io["tags"]["device"] = "8:0"
        self.assertEqual(convert(metrics + [unknown, per_cpu, per_net, per_io]), expected)
        self.assertIsNone(convert([unknown]))

    def test_mixed_server_or_hostname_batches_are_rejected(self):
        first = fixture("telegraf-host.json")["metrics"][0]
        for key in ("server_id", "host"):
            second = copy.deepcopy(first)
            second["tags"][key] = "other"
            with self.subTest(key=key), self.assertRaises(sender.DeliveryError):
                convert([first, second])

    def test_malformed_identity_timestamp_and_field_values_are_rejected(self):
        first = fixture("telegraf-host.json")["metrics"][0]
        for bad in (None, True, "1790413200", -1):
            metric = copy.deepcopy(first)
            metric["timestamp"] = bad
            with self.subTest(timestamp=bad), self.assertRaises(sender.DeliveryError):
                convert([metric])
        for bad in (None, True, "20", -1, float("inf"), float("nan")):
            metric = copy.deepcopy(first)
            metric["fields"]["usage_active"] = bad
            with self.subTest(value=bad), self.assertRaises(sender.DeliveryError):
                convert([metric])
        for bad in ({}, {"server_id": "production-01"}, {"server_id": "bad id", "host": "ubuntu-01"}):
            metric = copy.deepcopy(first)
            metric["tags"] = bad
            with self.subTest(tags=bad), self.assertRaises(sender.DeliveryError):
                convert([metric])

    def test_conflicting_same_resource_values_are_rejected(self):
        first = fixture("telegraf-host.json")["metrics"][0]
        second = copy.deepcopy(first)
        second["fields"]["usage_active"] = 99
        with self.assertRaises(sender.DeliveryError):
            convert([first, second])

    def test_equivalent_integer_and_fractional_percentages_are_deterministic(self):
        first = fixture("telegraf-host.json")["metrics"][0]
        second = copy.deepcopy(first)
        second["fields"]["usage_active"] = 23
        self.assertEqual(sender.format_payload(json.dumps({"metrics": [first, second]}).encode()),
                         sender.format_payload(json.dumps({"metrics": [second, first]}).encode()))

    def test_docker_health_identity_and_boolean_types_are_validated(self):
        rows = fixture("telegraf-docker.json")["metrics"]
        status = copy.deepcopy(next(row for row in rows if row["name"] == "docker_container_status"))
        status["fields"]["oomkilled"] = 0
        with self.assertRaises(sender.DeliveryError):
            convert([status])
        health = copy.deepcopy(next(row for row in rows if row["name"] == "docker_container_health"))
        health["tags"]["source"] = "not-an-id"
        with self.assertRaises(sender.DeliveryError):
            convert([health])

    def test_invalid_json_and_empty_batches_are_rejected(self):
        for payload in (b"", b"not-json", b"[]", b'{"metrics":[]}', b'{"metrics":[null]}',
                        b'{"metrics":[],"metrics":[]}', b"x" * (sender.MAX_BATCH_BYTES + 1)):
            with self.subTest(size=len(payload)), self.assertRaises(sender.DeliveryError):
                sender.format_payload(payload)


if __name__ == "__main__":
    unittest.main()
