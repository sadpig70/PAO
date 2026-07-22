import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from pao_helpers import REPO, RUNTIME_HOME, PaoTestCase
from pao_runtime import audit, oa_cli
from pao_runtime.common import FileLock, load_json, local_filesystem_status, utc_now
from pao_runtime.ledger import TaskLedger
from pao_runtime.oa_cli import renewable_oa_writer
from pao_runtime.transport import FileTransport


def old_file(path: Path, content: str = "old") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    stamp = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


class ContractAndRoutingRemediationTests(PaoTestCase):
    def test_f01_f02_dependency_requires_semantic_acceptance_and_workflow_is_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, upstream = self.send_task(
                root,
                "LWAR1",
                {"task_id": "task-upstream", "workflow_id": "workflow-safe", "goal": "upstream"},
            )
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, upstream["task_id"])
            self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            completed, _ = self.send_task(
                root,
                "LWAR1",
                {
                    "task_id": "task-downstream",
                    "workflow_id": "workflow-safe",
                    "goal": "downstream",
                    "depends_on": ["task-upstream"],
                },
                expected=None,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("dependency not satisfied", completed.stderr)
            self.run_module(
                "pao_runtime.oa_cli",
                "validate",
                "--task-id",
                upstream["task_id"],
                "--record",
                "--decision",
                "accepted",
                "--reason",
                "verified",
                "--root",
                str(root),
                expected=0,
            )
            self.send_task(
                root,
                "LWAR1",
                {
                    "task_id": "task-downstream",
                    "workflow_id": "workflow-safe",
                    "goal": "downstream",
                    "depends_on": ["task-upstream"],
                },
                expected=0,
            )
            with self.assertRaises(ValueError):
                TaskLedger(root).path("workflow-../../escape", "task-safe")

    def test_f04_stale_route_is_excluded_and_incoming_timeout_dead_letters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            draft = root / "auto.json"
            draft.write_text(json.dumps({"goal": "must not route stale"}), encoding="utf-8")
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "send",
                "--auto",
                "--require-capability",
                "coding",
                "--task-file",
                str(draft),
                "--root",
                str(root),
            )
            self.assertNotEqual(completed.returncode, 0)

            _, sent = self.send_task(root, "LWAR1", {"goal": "unclaimed timeout"})
            incoming = next((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            stamp = time.time() - 10
            os.utime(incoming, (stamp, stamp))
            _, recovered = self.run_module(
                "pao_runtime.oa_cli",
                "recover",
                "--delivery-timeout",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(recovered["incoming_expired"][0]["task_id"], sent["task_id"])
            self.assertTrue(list((root / "mailbox" / "LWAR1" / "dead").glob("*.json")))

    def test_f05_invalid_result_contract_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            outgoing = root / "mailbox" / "LWAR1" / "outgoing" / "task-bad.result.json"
            outgoing.write_text(
                json.dumps(
                    {
                        "schema_version": "pao.result.v1",
                        "task_id": "task-bad",
                        "lwar_id": "LWAR1",
                        "instance_id": identity["instance_id"],
                        "generation": identity["generation"],
                        "status": "succeeded",
                        "summary": "missing contract fields",
                        "evidence": {},
                    }
                ),
                encoding="utf-8",
            )
            _, collected = self.run_module(
                "pao_runtime.oa_cli", "collect", "--root", str(root), expected=0
            )
            self.assertEqual(collected["count"], 0)
            self.assertTrue(collected["quarantined"][0]["reason"].startswith("invalid_result_schema:"))


class TransactionRemediationTests(PaoTestCase):
    def test_f03_audit_failure_is_nonfatal_and_control_is_at_least_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            with mock.patch("builtins.open", side_effect=OSError("audit unavailable")):
                self.assertFalse(audit.record(root, "oa", {"event": "probe"}))
            real_open = open

            def fail_active_only(file, *args, **kwargs):
                if str(file).endswith("events.jsonl"):
                    raise OSError("active segment unavailable")
                return real_open(file, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=fail_active_only):
                self.assertFalse(audit.record(root, "oa", {"event": "spooled_probe"}))
            self.assertTrue((root / "var" / "audit" / "degraded.jsonl").is_file())
            self.assertTrue(audit.record(root, "oa", {"event": "recovery_probe"}))
            audit_events = [
                json.loads(line)["event"]
                for line in (root / "var" / "audit" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("spooled_probe", audit_events)
            self.assertIn("recovery_probe", audit_events)
            self.assertFalse((root / "var" / "audit" / "degraded.jsonl").exists())
            self.run_module(
                "pao_runtime.oa_cli",
                "control",
                "--lwar-id",
                "LWAR1",
                "--command",
                "ping",
                "--root",
                str(root),
                expected=0,
            )
            transport = FileTransport(root)
            first = transport.claim_control(identity)
            second = transport.claim_control(identity)
            self.assertEqual(first["control_id"], second["control_id"])
            transport.ack_control(identity, first)
            self.assertIsNone(transport.claim_control(identity))

    def test_audit_record_once_deduplicates_active_rotated_and_degraded_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            active = audit_dir / "events.jsonl"
            self.assertTrue(
                audit.record_once(root, "oa", {"event": "active_once"}, "key-active")
            )
            self.assertTrue(
                audit.record_once(root, "oa", {"event": "active_duplicate"}, "key-active")
            )
            rotated = audit_dir / "events.1.jsonl"
            active.replace(rotated)
            self.assertTrue(
                audit.record_once(root, "oa", {"event": "rotated_duplicate"}, "key-active")
            )

            degraded_event = {
                "schema_version": "pao.audit-event.v1",
                "ts": utc_now(),
                "actor": "oa",
                "event": "degraded_once",
                "idempotency_key": "key-degraded",
            }
            degraded_rotated_duplicate = {
                **degraded_event,
                "event": "rotated_backlog_duplicate",
                "idempotency_key": "key-active",
            }
            degraded = audit_dir / "degraded.jsonl"
            degraded.write_text(
                json.dumps(degraded_event)
                + "\n"
                + json.dumps(degraded_rotated_duplicate)
                + "\n",
                encoding="utf-8",
            )
            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "degraded_duplicate"},
                    "key-degraded",
                )
            )

            events = []
            for path in sorted(audit_dir.glob("events*.jsonl")):
                events.extend(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            self.assertEqual(
                sum(event.get("idempotency_key") == "key-active" for event in events),
                1,
            )
            self.assertEqual(
                sum(event.get("idempotency_key") == "key-degraded" for event in events),
                1,
            )
            self.assertFalse(degraded.exists())

    def test_audit_record_once_deduplicates_repeated_degraded_spool_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_open = open

            def fail_active_only(file, *args, **kwargs):
                if str(file).endswith("events.jsonl"):
                    raise OSError("active segment unavailable")
                return real_open(file, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=fail_active_only):
                self.assertFalse(
                    audit.record_once(root, "oa", {"event": "spooled_once"}, "key-spooled")
                )
                self.assertFalse(
                    audit.record_once(root, "oa", {"event": "spooled_retry"}, "key-spooled")
                )

            degraded = root / "var" / "audit" / "degraded.jsonl"
            degraded_events = [
                json.loads(line)
                for line in degraded.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(event.get("idempotency_key") == "key-spooled" for event in degraded_events),
                1,
            )

            self.assertTrue(
                audit.record_once(root, "oa", {"event": "recovered_retry"}, "key-spooled")
            )
            active_events = [
                json.loads(line)
                for line in (root / "var" / "audit" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(event.get("idempotency_key") == "key-spooled" for event in active_events),
                1,
            )
            self.assertFalse(degraded.exists())

    def test_audit_recovery_deduplicates_backlog_after_post_flush_process_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            audit_dir.mkdir(parents=True)
            degraded = audit_dir / "degraded.jsonl"
            degraded.write_text(
                json.dumps(
                    {
                        "schema_version": "pao.audit-event.v1",
                        "ts": utc_now(),
                        "actor": "oa",
                        "event": "degraded_before_crash",
                        "idempotency_key": "key-post-flush-crash",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            fault_code = (
                "import os,sys\n"
                "from pathlib import Path\n"
                "from pao_runtime import audit\n"
                "real_unlink=Path.unlink\n"
                "def crash_before_spool_delete(self,*args,**kwargs):\n"
                " if self.name=='degraded.jsonl':\n"
                "  os._exit(96)\n"
                " return real_unlink(self,*args,**kwargs)\n"
                "Path.unlink=crash_before_spool_delete\n"
                "audit.record_once(Path(sys.argv[1]),'oa',"
                "{'event':'retry_during_crash'},'key-post-flush-crash')\n"
            )
            crashed = subprocess.run(
                [sys.executable, "-c", fault_code, str(root)],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(RUNTIME_HOME)},
                check=False,
            )
            self.assertEqual(crashed.returncode, 96, crashed.stderr + crashed.stdout)
            self.assertTrue(degraded.is_file())

            active = audit_dir / "events.jsonl"
            crashed_events = [
                json.loads(line)
                for line in active.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == "key-post-flush-crash"
                    for event in crashed_events
                ),
                1,
            )

            stale_stamp = time.time() - 60
            for lock_name in (".audit.lock", ".degraded.lock"):
                lock_path = audit_dir / lock_name
                self.assertTrue(lock_path.is_file())
                os.utime(lock_path, (stale_stamp, stale_stamp))

            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "retry_after_crash"},
                    "key-post-flush-crash",
                )
            )
            recovered_events = [
                json.loads(line)
                for line in active.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == "key-post-flush-crash"
                    for event in recovered_events
                ),
                1,
            )
            self.assertFalse(degraded.exists())

    def test_audit_active_fsync_failure_preserves_degraded_record_for_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(
                audit.os,
                "fsync",
                side_effect=[OSError("active fsync unavailable"), None],
            ) as fsync:
                self.assertFalse(
                    audit.record_once(
                        root,
                        "oa",
                        {"event": "fsync_fault"},
                        "key-fsync-fault",
                    )
                )
            self.assertEqual(fsync.call_count, 2)

            audit_dir = root / "var" / "audit"
            degraded = audit_dir / "degraded.jsonl"
            degraded_events = [
                json.loads(line)
                for line in degraded.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == "key-fsync-fault"
                    for event in degraded_events
                ),
                1,
            )

            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "fsync_recovery"},
                    "key-fsync-fault",
                )
            )
            active_events = [
                json.loads(line)
                for line in (audit_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == "key-fsync-fault"
                    for event in active_events
                ),
                1,
            )
            self.assertFalse(degraded.exists())

    def test_audit_prune_preserves_rotated_key_until_degraded_replay_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            keyed_event = {
                "schema_version": "pao.audit-event.v1",
                "ts": utc_now(),
                "actor": "oa",
                "event": "pending_prune_guard",
                "idempotency_key": "key-prune-guard",
            }
            rotated = old_file(
                audit_dir / "events.1.jsonl",
                json.dumps(keyed_event) + "\n",
            )
            degraded = audit_dir / "degraded.jsonl"
            degraded.write_text(json.dumps(keyed_event) + "\n", encoding="utf-8")
            cutoff = datetime.now(timezone.utc)

            self.assertEqual(audit.prune_rotated(root, cutoff), 0)
            self.assertTrue(rotated.is_file())
            self.assertTrue(degraded.is_file())

            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "prune_guard_recovery"},
                    "key-prune-guard",
                )
            )
            events = []
            for path in sorted(audit_dir.glob("events*.jsonl")):
                events.extend(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            self.assertEqual(
                sum(event.get("idempotency_key") == "key-prune-guard" for event in events),
                1,
            )
            self.assertFalse(degraded.exists())
            self.assertEqual(audit.prune_rotated(root, cutoff), 1)
            self.assertFalse(rotated.exists())

    def test_audit_prune_waits_for_degraded_lock_under_audit_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            rotated = old_file(audit_dir / "events.1.jsonl", "{}\n")
            audit_lock = audit_dir / ".audit.lock"
            degraded_lock = audit_dir / ".degraded.lock"
            cutoff = datetime.now(timezone.utc)

            with ThreadPoolExecutor(max_workers=1) as executor:
                with FileLock(degraded_lock):
                    future = executor.submit(audit.prune_rotated, root, cutoff)
                    deadline = time.monotonic() + 2
                    while not audit_lock.is_file() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(audit_lock.is_file())
                    self.assertFalse(future.done())
                self.assertEqual(future.result(timeout=2), 1)
            self.assertFalse(rotated.exists())

    def test_audit_key_scan_fails_closed_for_unreadable_active_and_rotated_segments(self):
        for segment_name in ("events.jsonl", "events.1.jsonl"):
            with self.subTest(segment=segment_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                audit_dir = root / "var" / "audit"
                audit_dir.mkdir(parents=True)
                segment = audit_dir / segment_name
                segment.write_text(
                    json.dumps(
                        {
                            "schema_version": "pao.audit-event.v1",
                            "ts": utc_now(),
                            "actor": "oa",
                            "event": "committed_before_read_fault",
                            "idempotency_key": "key-unreadable-segment",
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                real_path_open = Path.open

                def fail_target_open(path, *args, **kwargs):
                    if path == segment:
                        raise OSError("audit segment temporarily unreadable")
                    return real_path_open(path, *args, **kwargs)

                with mock.patch.object(Path, "open", fail_target_open):
                    self.assertFalse(
                        audit.record_once(
                            root,
                            "oa",
                            {"event": "retry_during_read_fault"},
                            "key-unreadable-segment",
                        )
                    )

                degraded = audit_dir / "degraded.jsonl"
                degraded_events = [
                    json.loads(line)
                    for line in degraded.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertEqual(
                    sum(
                        event.get("idempotency_key") == "key-unreadable-segment"
                        for event in degraded_events
                    ),
                    1,
                )

                self.assertTrue(
                    audit.record_once(
                        root,
                        "oa",
                        {"event": "retry_after_read_fault"},
                        "key-unreadable-segment",
                    )
                )
                recovered_events = []
                for path in sorted(audit_dir.glob("events*.jsonl")):
                    recovered_events.extend(
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    )
                self.assertEqual(
                    sum(
                        event.get("idempotency_key") == "key-unreadable-segment"
                        for event in recovered_events
                    ),
                    1,
                )
                self.assertFalse(degraded.exists())

    def test_audit_repairs_and_quarantines_only_a_crash_truncated_active_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            audit_dir.mkdir(parents=True)
            active = audit_dir / "events.jsonl"
            valid_unkeyed = json.dumps(
                {
                    "schema_version": "pao.audit-event.v1",
                    "ts": utc_now(),
                    "actor": "oa",
                    "event": "valid_unkeyed_before_tail",
                }
            )
            truncated = b'{"schema_version":"pao.audit-event.v1","idempotency_key":"lost'
            active.write_bytes(valid_unkeyed.encode("utf-8") + b"\n" + truncated)

            health = audit.health(root)
            self.assertEqual(health["status"], "blocked")
            self.assertTrue(health["segments"][0]["repairable_truncated_tail"])
            self.assertTrue(any("bounded tail repair" in item for item in health["guidance"]))
            self.assertEqual(active.read_bytes(), valid_unkeyed.encode("utf-8") + b"\n" + truncated)

            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "after_tail_repair"},
                    "key-after-tail-repair",
                )
            )
            active_events = [
                json.loads(line)
                for line in active.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual([event["event"] for event in active_events], [
                "valid_unkeyed_before_tail",
                "after_tail_repair",
            ])
            quarantined = list((audit_dir / ".corrupt").glob("events.jsonl.*.tail"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), truncated)

    def test_audit_malformed_rotated_line_fails_closed_until_operator_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            audit_dir.mkdir(parents=True)
            rotated = audit_dir / "events.1.jsonl"
            valid_unkeyed = json.dumps(
                {
                    "schema_version": "pao.audit-event.v1",
                    "ts": utc_now(),
                    "actor": "oa",
                    "event": "valid_rotated_prefix",
                }
            )
            rotated.write_text(valid_unkeyed + "\n{malformed}\n", encoding="utf-8")

            self.assertFalse(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "blocked_by_malformed_rotated"},
                    "key-after-operator-repair",
                )
            )
            degraded = audit_dir / "degraded.jsonl"
            self.assertTrue(degraded.is_file())
            self.assertEqual(rotated.read_text(encoding="utf-8"), valid_unkeyed + "\n{malformed}\n")

            rotated.write_text(valid_unkeyed + "\n", encoding="utf-8")
            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "after_operator_repair"},
                    "key-after-operator-repair",
                )
            )
            events = []
            for path in sorted(audit_dir.glob("events*.jsonl")):
                events.extend(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == "key-after-operator-repair"
                    for event in events
                ),
                1,
            )
            self.assertFalse(degraded.exists())

    def test_audit_health_reports_blocked_replay_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            audit_dir.mkdir(parents=True)
            active = audit_dir / "events.jsonl"
            active.write_text("{malformed}\n", encoding="utf-8")
            degraded = audit_dir / "degraded.jsonl"
            degraded.write_text(
                json.dumps(
                    {
                        "schema_version": "pao.audit-event.v1",
                        "ts": utc_now(),
                        "actor": "oa",
                        "event": "pending_health_probe",
                        "idempotency_key": "key-health-probe",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            corrupt = audit_dir / ".corrupt" / "events.jsonl.1.tail"
            corrupt.parent.mkdir()
            corrupt.write_bytes(b"tail")
            before = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }

            _, report = self.run_module(
                "pao_runtime.oa_cli",
                "audit-health",
                "--root",
                str(root),
                expected=2,
            )

            self.assertEqual(report["event"], "audit_health")
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(report["keyed_append_blocked"])
            self.assertTrue(report["blocked_replay"])
            self.assertEqual(report["segments"][0]["status"], "malformed")
            self.assertEqual(report["segments"][0]["malformed_lines"], [1])
            self.assertFalse(report["segments"][0]["repairable_truncated_tail"])
            self.assertEqual(
                report["repair_candidates"],
                [
                    {
                        "segment": "events.jsonl",
                        "expected_sha256": hashlib.sha256(active.read_bytes()).hexdigest(),
                        "drop_lines": [1],
                    }
                ],
            )
            self.assertEqual(report["degraded"]["status"], "healthy")
            self.assertEqual(report["pending_count"], 1)
            self.assertEqual(report["quarantined_fragments"][0]["bytes"], 4)
            self.assertTrue(any("no automatic repair is safe" in item for item in report["guidance"]))
            after = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
            self.assertFalse((root / "var" / "oa").exists())

    def test_audit_health_reports_healthy_valid_unkeyed_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            audit_dir.mkdir(parents=True)
            (audit_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "pao.audit-event.v1",
                        "ts": utc_now(),
                        "actor": "oa",
                        "event": "healthy_unkeyed",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            _, report = self.run_module(
                "pao_runtime.oa_cli",
                "audit-health",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(report["status"], "healthy")
            self.assertFalse(report["keyed_append_blocked"])
            self.assertFalse(report["blocked_replay"])
            self.assertEqual(report["segments"][0]["keyed_count"], 0)
            self.assertEqual(report["guidance"], ["No action required."])

    def test_audit_repair_dogfood_restores_replay_once_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            audit_dir.mkdir(parents=True)
            valid = json.dumps(
                {
                    "schema_version": "pao.audit-event.v1",
                    "ts": utc_now(),
                    "actor": "oa",
                    "event": "valid_before_repair",
                }
            )
            original = (valid + "\n{malformed}\n").encode("utf-8")
            rotated = audit_dir / "events.1.jsonl"
            rotated.write_bytes(original)
            pending = {
                "schema_version": "pao.audit-event.v1",
                "ts": utc_now(),
                "actor": "oa",
                "event": "pending_repair_dogfood",
                "idempotency_key": "key-repair-dogfood",
            }
            (audit_dir / "degraded.jsonl").write_text(
                json.dumps(pending) + "\n", encoding="utf-8"
            )
            _, blocked = self.run_module(
                "pao_runtime.oa_cli", "audit-health", "--root", str(root), expected=2
            )
            self.assertTrue(blocked["blocked_replay"])
            repair_candidate = blocked["repair_candidates"][0]
            self.assertEqual(repair_candidate["segment"], "events.1.jsonl")
            self.assertEqual(repair_candidate["drop_lines"], [2])
            digest = repair_candidate["expected_sha256"]
            self.assertEqual(digest, hashlib.sha256(original).hexdigest())

            _, repaired = self.run_module(
                "pao_runtime.oa_cli",
                "audit-repair",
                "--segment",
                "events.1.jsonl",
                "--expected-sha256",
                digest,
                "--drop-line",
                "2",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(repaired["event"], "audit_segment_repaired")
            self.assertEqual(repaired["health_status"], "attention")
            self.assertFalse(repaired["keyed_append_blocked"])
            self.assertFalse(repaired["blocked_replay"])
            self.assertEqual(repaired["pending_count"], 0)
            self.assertTrue(repaired["audit_event_committed"])
            self.assertEqual(rotated.read_bytes(), (valid + "\n").encode("utf-8"))
            backup = audit_dir / repaired["backup"]
            self.assertEqual(backup.read_bytes(), original)
            self.assertFalse((audit_dir / "degraded.jsonl").exists())

            events = []
            for path in sorted(audit_dir.glob("events*.jsonl")):
                events.extend(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            self.assertEqual(
                sum(event.get("idempotency_key") == "key-repair-dogfood" for event in events),
                1,
            )
            self.assertEqual(
                sum(event.get("event") == "audit_repair_committed" for event in events),
                1,
            )
            _, health = self.run_module(
                "pao_runtime.oa_cli", "audit-health", "--root", str(root), expected=0
            )
            self.assertEqual(health["status"], "attention")
            self.assertFalse(health["keyed_append_blocked"])

            before_retry = {
                path.relative_to(audit_dir).as_posix(): path.read_bytes()
                for path in audit_dir.rglob("*")
                if path.is_file()
            }
            retry, _ = self.run_module(
                "pao_runtime.oa_cli",
                "audit-repair",
                "--segment",
                "events.1.jsonl",
                "--expected-sha256",
                digest,
                "--drop-line",
                "2",
                "--root",
                str(root),
            )
            self.assertNotEqual(retry.returncode, 0)
            self.assertIn("fingerprint changed", retry.stderr)
            after_retry = {
                path.relative_to(audit_dir).as_posix(): path.read_bytes()
                for path in audit_dir.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after_retry, before_retry)

    def test_audit_repair_rejects_drift_partial_valid_and_unsafe_segment(self):
        valid = json.dumps({"event": "valid"})
        original = (valid + "\n{bad-one}\n{bad-two}\n").encode("utf-8")
        digest = hashlib.sha256(original).hexdigest()
        cases = (
            ("events.1.jsonl", "0" * 64, [2, 3], "fingerprint changed"),
            ("events.1.jsonl", digest, [2], "exactly match"),
            ("events.1.jsonl", digest, [1, 2, 3], "exactly match"),
            ("events.1.jsonl", digest, [2, 2, 3], "duplicates"),
            ("../events.1.jsonl", digest, [2, 3], "segment must be"),
        )
        for segment, expected, selected, message in cases:
            with self.subTest(segment=segment, selected=selected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "var" / "audit" / "events.1.jsonl"
                target.parent.mkdir(parents=True)
                target.write_bytes(original)
                with self.assertRaisesRegex((OSError, ValueError), message):
                    audit.repair(root, segment, expected, selected)
                self.assertEqual(target.read_bytes(), original)
                self.assertFalse((target.parent / ".corrupt").exists())

    def test_audit_repair_requires_the_oa_writer_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "var" / "audit" / "events.1.jsonl"
            target.parent.mkdir(parents=True)
            original = b"{bad}\n"
            target.write_bytes(original)
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "audit-repair",
                "--segment",
                "events.1.jsonl",
                "--expected-sha256",
                hashlib.sha256(original).hexdigest(),
                "--drop-line",
                "1",
                "--root",
                str(root),
                env={"PAO_OA_ID": ""},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("PAO_OA_ID is required", completed.stderr)
            self.assertEqual(target.read_bytes(), original)
            self.assertFalse((target.parent / ".corrupt").exists())

    def test_audit_repair_preserves_original_before_failed_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "var" / "audit" / "events.1.jsonl"
            target.parent.mkdir(parents=True)
            original = b'{"event":"valid"}\n{bad}\n'
            target.write_bytes(original)
            digest = hashlib.sha256(original).hexdigest()
            with mock.patch.object(audit, "_replace_retry", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    audit.repair(root, "events.1.jsonl", digest, [2])
            self.assertEqual(target.read_bytes(), original)
            backups = list((target.parent / ".corrupt").glob("*.repair-original"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertFalse(list(target.parent.glob("*.repair-*.tmp")))

    def test_f06_interrupted_publication_is_repaired_from_ledger_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "repair publication"})
            incoming = next((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            incoming.unlink()
            ledger = TaskLedger(root)
            workflow_id = ledger.get(sent["task_id"])["workflow_id"]
            ledger.transition(sent["task_id"], "publishing", workflow_id, detail="crash probe")
            _, recovered = self.run_module(
                "pao_runtime.oa_cli", "recover", "--root", str(root), expected=0
            )
            self.assertEqual(recovered["publication_repaired"][0]["task_id"], sent["task_id"])
            self.assertEqual(ledger.get(sent["task_id"])["status"], "published")
            self.assertTrue(list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json")))

    def test_f07_archive_before_ledger_commit_is_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "archive reconciliation"})
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, sent["task_id"])
            transport = FileTransport(root)
            outgoing = next((root / "mailbox" / "LWAR1" / "outgoing").glob("*.json"))
            archived = transport.archive_result("LWAR1", outgoing)
            _, collected = self.run_module(
                "pao_runtime.oa_cli", "collect", "--archive", "--root", str(root), expected=0
            )
            entry = TaskLedger(root).get(sent["task_id"])
            self.assertEqual(entry["status"], "completed")
            self.assertEqual(entry["result_file"], str(archived))
            self.assertEqual(collected["count"], 1)

    def test_f08_recovery_fences_a_superseded_claim_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "fence stale attempt"})
            _, first = self.watch_once(root, identity, expected=0)
            old_token = first["task"]["claim_token"]
            self.expire_lease(root, "LWAR1", sent["task_id"])
            self.run_module("pao_runtime.oa_cli", "recover", "--root", str(root), expected=0)
            _, second = self.watch_once(root, identity, expected=0)
            self.assertNotEqual(old_token, second["task"]["claim_token"])
            result_path = root / "stale-result.json"
            result_path.write_text(
                json.dumps({"status": "succeeded", "summary": "late", "evidence": {}, "artifacts": []}),
                encoding="utf-8",
            )
            completed, _ = self.run_module(
                "pao_runtime.lwar_cli",
                "complete",
                "--identity-file",
                identity["identity_file"],
                "--task-id",
                sent["task_id"],
                "--claim-token",
                old_token,
                "--result-file",
                str(result_path),
                "--root",
                str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("claim superseded", completed.stderr)


class OperationalRemediationTests(PaoTestCase):
    def test_f09_mutations_require_identity_and_writer_lease_renews(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "reconcile",
                "--root",
                str(root),
                env={"PAO_OA_ID": ""},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("PAO_OA_ID is required", completed.stderr)
            with mock.patch.dict(os.environ, {"PAO_OA_ID": "oa-renew-test"}):
                with renewable_oa_writer(root, ttl_s=2):
                    lease_path = root / "var" / "oa" / "writer_lease.json"
                    before = load_json(lease_path)["refreshed_at"]
                    time.sleep(1.2)
                    after = load_json(lease_path)["refreshed_at"]
                    self.assertNotEqual(before, after)

    def test_writer_renewal_deadline_does_not_accumulate_refresh_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stamps = []
            ensure_calls = 0
            original_ensure = oa_cli.ensure_oa_writer

            def delayed_ensure(target, ttl_s):
                nonlocal ensure_calls
                lease = original_ensure(target, ttl_s)
                ensure_calls += 1
                if ensure_calls > 1:
                    time.sleep(0.4)
                return lease

            def record_presence(target, oa_id, ttl_s=90.0):
                stamps.append(time.monotonic())
                return {"oa_id": oa_id}

            with mock.patch.dict(os.environ, {"PAO_OA_ID": "oa-deadline-test"}):
                with mock.patch.object(oa_cli, "ensure_oa_writer", side_effect=delayed_ensure):
                    with mock.patch.object(
                        oa_cli, "publish_oa_presence", side_effect=record_presence
                    ):
                        with oa_cli.renewable_oa_writer(root, ttl_s=3):
                            time.sleep(2.6)

            self.assertGreaterEqual(len(stamps), 3)
            self.assertLess(
                stamps[2] - stamps[1],
                1.2,
                "renewal latency must not be added to the next wait interval",
            )

    def test_f10_installer_discovers_and_copies_only_canonical_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "installed"
            _, info = self.run_module("pao_runtime.pao_cli", "info", expected=0)
            self.assertEqual(Path(info["skills_source"]).resolve(), REPO / ".agents" / "skills")
            _, installed = self.run_module(
                "pao_runtime.pao_cli",
                "install-skills",
                "--target",
                str(target),
                expected=0,
            )
            self.assertEqual({item["skill"] for item in installed["skills"]}, {"pao-oa", "pao-lwar"})
            self.assertEqual({path.name for path in target.iterdir()}, {"pao-oa", "pao-lwar"})

    def test_f11_transactional_bundle_sync_preserves_authored_files(self):
        spec = importlib.util.spec_from_file_location("sync_bundles", REPO / "tools" / "sync_bundles.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            master = base / "pao-lwar"
            mirror = base / "pao-oa"
            for bundle in (master, mirror):
                for name in module.MIRRORED:
                    (bundle / name).mkdir(parents=True)
                    (bundle / name / "payload.txt").write_text(bundle.name, encoding="utf-8")
            (mirror / "SKILL.md").write_text("authored", encoding="utf-8")
            self.assertTrue(module.bundle_diff(master, mirror))
            module.transactional_sync(master, mirror)
            self.assertFalse(module.bundle_diff(master, mirror))
            self.assertEqual((mirror / "SKILL.md").read_text(encoding="utf-8"), "authored")

    def test_bundle_sync_falls_back_when_windows_holds_the_mirror_root(self):
        spec = importlib.util.spec_from_file_location("sync_bundles", REPO / "tools" / "sync_bundles.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            master = base / "pao-lwar"
            mirror = base / "pao-oa"
            for bundle in (master, mirror):
                for name in module.MIRRORED:
                    (bundle / name).mkdir(parents=True)
                    (bundle / name / "payload.txt").write_text(bundle.name, encoding="utf-8")
            (mirror / "SKILL.md").write_text("authored", encoding="utf-8")
            real_replace = os.replace

            def deny_root_rename(source, destination):
                if Path(source) == mirror:
                    raise PermissionError("mirror root is open")
                return real_replace(source, destination)

            with mock.patch.object(module.os, "replace", side_effect=deny_root_rename):
                module.transactional_sync(master, mirror)

            self.assertFalse(module.bundle_diff(master, mirror))
            self.assertEqual((mirror / "SKILL.md").read_text(encoding="utf-8"), "authored")

    def test_f12_result_artifact_content_cannot_expose_runtime_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "privacy enforcement"})
            self.watch_once(root, identity, expected=0)
            artifact = root / "neutral.txt"
            artifact.write_text("generated by Test Model", encoding="utf-8")
            result = {
                "status": "succeeded",
                "summary": "done",
                "evidence": {"ok": True},
                "artifacts": [str(artifact)],
            }
            completed, _ = self.complete_task(root, identity, sent["task_id"], result, expected=None)
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("artifact content exposes runtime identity", completed.stderr)

    def test_f13_doctor_requires_local_filesystem(self):
        with tempfile.TemporaryDirectory() as directory:
            local, _ = local_filesystem_status(Path(directory))
            self.assertTrue(local)
            _, report = self.run_module(
                "pao_runtime.pao_cli", "doctor", "--root", str(Path(directory) / "bus"), expected=0
            )
            checks = {item["check"]: item["ok"] for item in report["checks"]}
            self.assertTrue(checks["local_filesystem"])
        if os.name == "nt":
            remote, detail = local_filesystem_status(Path(r"\\server\share"))
            self.assertFalse(remote)
            self.assertIn("UNC", detail)

    def test_f14_prune_bounds_tombstones_artifacts_and_audit_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "retained artifact"})
            self.watch_once(root, identity, expected=0)
            artifact = root / "retained.txt"
            artifact.write_text("neutral evidence", encoding="utf-8")
            self.complete_task(
                root,
                identity,
                sent["task_id"],
                {
                    "status": "succeeded",
                    "summary": "done",
                    "evidence": {"ok": True},
                    "artifacts": [str(artifact)],
                },
            )
            self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            snapshot = root / TaskLedger(root).get(sent["task_id"])["result"]["artifacts"][0]["snapshot"]
            stamp = time.time() - 2 * 86400
            os.utime(snapshot, (stamp, stamp))
            unreferenced = old_file(root / "var" / "artifacts" / ("f" * 64))
            rotated = old_file(root / "var" / "audit" / "events.1.jsonl", "{}\n")
            tombstone = old_file(root / "mailbox" / "LWAR1" / "cancelled" / "task-old.json", "{}")
            dead = old_file(root / "mailbox" / "LWAR1" / "dead" / "005_task-dead.json", "{}")
            self.run_module(
                "pao_runtime.oa_cli",
                "prune",
                "--older-than-days",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertTrue(snapshot.is_file())
            self.assertFalse(unreferenced.exists())
            self.assertFalse(rotated.exists())
            self.assertFalse(tombstone.exists())
            self.assertTrue(dead.exists())
            self.assertTrue((root / "var" / "audit" / "events.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
