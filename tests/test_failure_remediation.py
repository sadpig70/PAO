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
    def assert_rotated_counts(self, report, removed, protected, blocked):
        self.assertEqual(report["audit_segments_removed"], removed)
        self.assertEqual(report["audit_segments_protected"], protected)
        self.assertEqual(report["audit_segments_blocked"], blocked)
        self.assertEqual(
            len(report["audit_segment_outcomes"]),
            removed + protected + blocked,
        )

    def _create_old_committed_repair(self, root: Path):
        target = root / "var" / "audit" / "events.1.jsonl"
        target.parent.mkdir(parents=True)
        original = b'{"event":"valid"}\n{bad}\n'
        target.write_bytes(original)
        digest = hashlib.sha256(original).hexdigest()
        _, repaired = self.run_module(
            "pao_runtime.oa_cli",
            "audit-repair",
            "--segment",
            target.name,
            "--expected-sha256",
            digest,
            "--drop-line",
            "2",
            "--root",
            str(root),
            expected=0,
        )
        receipt_path = target.parent / repaired["receipt"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["committed_at"] = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat().replace("+00:00", "Z")
        receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        return target, receipt_path, target.parent / receipt["backup"]

    def _create_applied_recreated_prune(self, root: Path):
        audit_dir = root / "var" / "audit"
        target = old_file(audit_dir / "events.1.jsonl", "{}\n")
        pruned = audit.prune_rotated(root, datetime.now(timezone.utc))
        self.assertFalse(target.exists())
        receipt_path = root / pruned["audit_prune_receipt"]
        receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        recreated = b'{"event":"recreated"}\n'
        target.write_bytes(recreated)
        old_stamp = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).timestamp()
        os.utime(target, (old_stamp, old_stamp))
        return (
            target,
            receipt_path,
            receipt_sha256,
            hashlib.sha256(recreated).hexdigest(),
            pruned,
        )

    def _create_resolved_preservation(self, root: Path):
        (
            target,
            receipt_path,
            receipt_sha256,
            segment_sha256,
            pruned,
        ) = self._create_applied_recreated_prune(root)
        _, resolved = self.run_module(
            "pao_runtime.oa_cli",
            "audit-prune-resolve",
            "--run-id",
            pruned["audit_prune_run_id"],
            "--expected-receipt-sha256",
            receipt_sha256,
            "--segment",
            target.name,
            "--expected-segment-sha256",
            segment_sha256,
            "--decision",
            "preserve-recreated",
            "--root",
            str(root),
            expected=0,
        )
        self.assertFalse(receipt_path.exists())
        marker_path = target.parent / resolved["preservation"]
        return target, marker_path, resolved

    def _preservation_release_fence(self, target: Path, marker_path: Path):
        marker = audit._load_rotated_preservation(marker_path)
        return {
            "run_id": marker["run_id"],
            "segment": marker["segment"],
            "marker_sha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            "segment_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "decision": "release-protection",
        }

    def _create_event_first_preservation_release(
        self, root: Path, target: Path, marker_path: Path
    ):
        fence = self._preservation_release_fence(target, marker_path)
        prepared = audit.prepare_rotated_preservation_release(
            root,
            run_id=fence["run_id"],
            segment=fence["segment"],
            expected_marker_sha256=fence["marker_sha256"],
            expected_segment_sha256=fence["segment_sha256"],
            decision=fence["decision"],
        )
        payload = {
            "event": "audit_preservation_released",
            "decision": prepared["decision"],
            "run_id": prepared["run_id"],
            "segment": prepared["segment"],
            "preservation": prepared["preservation"],
            "marker_sha256": prepared["marker_sha256"],
            "preserved_sha256": prepared["preserved_sha256"],
            "preserved_bytes": prepared["preserved_bytes"],
            "pruned_audit_key": prepared["pruned_audit_key"],
            "resolution_audit_key": prepared["resolution_audit_key"],
        }
        self.assertTrue(
            audit.record_once(
                root,
                "oa",
                payload,
                prepared["release_audit_key"],
            )
        )
        return fence, prepared

    def _authorize_repair_retention(self, root: Path, receipt_path: Path) -> Path:
        real_unlink = Path.unlink

        def refuse_receipt_unlink(path, *args, **kwargs):
            if path == receipt_path:
                raise OSError("stop after retention authorization")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", refuse_receipt_unlink):
            counts = audit.prune_committed_repairs(root, datetime.now(timezone.utc))
        self.assertEqual(
            counts,
            {"repair_receipts_removed": 0, "repair_backups_removed": 0},
        )
        return next((receipt_path.parents[1] / ".repair-prune").glob("*.json"))

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

            protected = audit.prune_rotated(root, cutoff)
            self.assert_rotated_counts(protected, 0, 1, 0)
            self.assertEqual(
                protected["audit_segment_outcomes"],
                [
                    {
                        "path": "var/audit/events.1.jsonl",
                        "status": "protected",
                        "reason_codes": ["degraded_replay_key"],
                    }
                ],
            )
            self.assertTrue(rotated.is_file())
            self.assertTrue(degraded.is_file())

            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "pruned", **protected},
                    protected["audit_prune_audit_key"],
                )
            )
            self.assertTrue(audit.commit_rotated_prune_receipt(root, protected))
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
            removed = audit.prune_rotated(root, cutoff)
            self.assert_rotated_counts(removed, 1, 0, 0)
            self.assertEqual(
                removed["audit_segment_outcomes"],
                [
                    {
                        "path": "var/audit/events.1.jsonl",
                        "status": "removed",
                        "reason_codes": ["valid_expired"],
                    }
                ],
            )
            self.assertFalse(rotated.exists())

    def test_audit_prune_preserves_retention_key_carrier_and_rotated_target(self):
        for state in ("resumable", "blocked"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, receipt_path, backup = self._create_old_committed_repair(root)
                audit_dir = target.parent
                tombstone = self._authorize_repair_retention(root, receipt_path)
                if state == "blocked":
                    backup.write_bytes(b"drifted retained backup")

                key_carrier = audit_dir / "events.2.jsonl"
                os.replace(audit_dir / "events.jsonl", key_carrier)
                unrelated = audit_dir / "events.3.jsonl"
                unrelated.write_text(
                    json.dumps({"event": "unrelated_old_segment"}) + "\n",
                    encoding="utf-8",
                )
                old_stamp = (
                    datetime.now(timezone.utc) - timedelta(days=2)
                ).timestamp()
                for path in (target, key_carrier, unrelated):
                    os.utime(path, (old_stamp, old_stamp))

                report = audit.prune_rotated(root, datetime.now(timezone.utc))
                self.assert_rotated_counts(report, 1, 2, 0)
                self.assertEqual(
                    report["audit_segment_outcomes"],
                    [
                        {
                            "path": "var/audit/events.1.jsonl",
                            "status": "protected",
                            "reason_codes": ["retention_target"],
                        },
                        {
                            "path": "var/audit/events.2.jsonl",
                            "status": "protected",
                            "reason_codes": ["retention_audit_key"],
                        },
                        {
                            "path": "var/audit/events.3.jsonl",
                            "status": "removed",
                            "reason_codes": ["valid_expired"],
                        },
                    ],
                )
                self.assertTrue(target.is_file())
                self.assertTrue(key_carrier.is_file())
                self.assertFalse(unrelated.exists())
                self.assertTrue(tombstone.is_file())

    def test_audit_prune_releases_rotation_fences_after_retention_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, receipt_path, _ = self._create_old_committed_repair(root)
            audit_dir = target.parent
            tombstone = self._authorize_repair_retention(root, receipt_path)
            key_carrier = audit_dir / "events.2.jsonl"
            os.replace(audit_dir / "events.jsonl", key_carrier)
            old_stamp = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).timestamp()
            for path in (target, key_carrier):
                os.utime(path, (old_stamp, old_stamp))

            counts = audit.prune_committed_repairs(
                root, datetime.now(timezone.utc) - timedelta(days=100)
            )
            self.assertEqual(counts["repair_backups_removed"], 1)
            self.assertFalse(tombstone.exists())
            report = audit.prune_rotated(root, datetime.now(timezone.utc))
            self.assert_rotated_counts(report, 2, 0, 0)
            self.assertEqual(
                report["audit_segment_outcomes"],
                [
                    {
                        "path": "var/audit/events.1.jsonl",
                        "status": "removed",
                        "reason_codes": ["valid_expired"],
                    },
                    {
                        "path": "var/audit/events.2.jsonl",
                        "status": "removed",
                        "reason_codes": ["valid_expired"],
                    },
                ],
            )
            self.assertFalse(target.exists())
            self.assertFalse(key_carrier.exists())

    def test_audit_prune_fails_closed_when_retention_keys_cannot_be_loaded(self):
        cases = ("malformed", "unreadable", "nonfile")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                audit_dir = root / "var" / "audit"
                first = old_file(audit_dir / "events.1.jsonl", "{}\n")
                second = old_file(audit_dir / "events.2.jsonl", "{}\n")
                retention = audit_dir / ".repair-prune"
                retention.mkdir()
                tombstone = retention / "unreadable.json"
                if case == "nonfile":
                    tombstone.mkdir()
                else:
                    tombstone.write_text("{invalid}\n", encoding="utf-8")

                if case == "unreadable":
                    with mock.patch.object(
                        audit,
                        "_load_repair_retention",
                        side_effect=OSError("retention unavailable"),
                    ):
                        removed = audit.prune_rotated(
                            root, datetime.now(timezone.utc)
                        )
                else:
                    removed = audit.prune_rotated(
                        root, datetime.now(timezone.utc)
                    )
                self.assert_rotated_counts(removed, 0, 0, 2)
                self.assertEqual(
                    removed["audit_segment_outcomes"],
                    [
                        {
                            "path": "var/audit/events.1.jsonl",
                            "status": "blocked",
                            "reason_codes": ["retention_snapshot_invalid"],
                        },
                        {
                            "path": "var/audit/events.2.jsonl",
                            "status": "blocked",
                            "reason_codes": ["retention_snapshot_invalid"],
                        },
                    ],
                )
                self.assertTrue(first.is_file())
                self.assertTrue(second.is_file())

    def test_audit_prune_validates_every_rotated_segment_before_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            valid = old_file(audit_dir / "events.1.jsonl", "{}\n")
            malformed = old_file(audit_dir / "events.2.jsonl", "{bad}\n")
            nonobject = old_file(audit_dir / "events.3.jsonl", "[]\n")
            unreadable = old_file(audit_dir / "events.4.jsonl", "{}\n")
            unlink_blocked = old_file(audit_dir / "events.5.jsonl", "{}\n")
            stat_blocked = old_file(audit_dir / "events.6.jsonl", "{}\n")
            invalid_utf8 = audit_dir / "events.7.jsonl"
            invalid_utf8.write_bytes(b"\xff\n")
            disappeared = old_file(audit_dir / "events.8.jsonl", "{}\n")
            old_stamp = (
                datetime.now(timezone.utc) - timedelta(days=2)
            ).timestamp()
            os.utime(invalid_utf8, (old_stamp, old_stamp))
            real_segment_keys = audit._rotated_segment_keys
            real_unlink = Path.unlink
            real_stat = Path.stat

            def fail_one_key_read(path):
                if path == unreadable:
                    return (
                        None,
                        "segment_unreadable",
                        "segment bytes unavailable",
                        None,
                        None,
                    )
                return real_segment_keys(path)

            def fail_one_unlink(path, *args, **kwargs):
                if path == unlink_blocked:
                    raise PermissionError("segment held open")
                return real_unlink(path, *args, **kwargs)

            def fail_one_stat(path, *args, **kwargs):
                if path == stat_blocked:
                    raise OSError("segment metadata unavailable")
                if path == disappeared:
                    raise FileNotFoundError("segment disappeared")
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(
                audit, "_rotated_segment_keys", side_effect=fail_one_key_read
            ):
                with mock.patch.object(Path, "unlink", fail_one_unlink):
                    with mock.patch.object(Path, "stat", fail_one_stat):
                        counts = audit.prune_rotated(
                            root, datetime.now(timezone.utc)
                        )

            self.assert_rotated_counts(counts, 1, 0, 7)
            self.assertEqual(
                [
                    (outcome["path"], outcome["status"], outcome["reason_codes"])
                    for outcome in counts["audit_segment_outcomes"]
                ],
                [
                    (
                        "var/audit/events.1.jsonl",
                        "removed",
                        ["valid_expired"],
                    ),
                    (
                        "var/audit/events.2.jsonl",
                        "blocked",
                        ["malformed_jsonl"],
                    ),
                    (
                        "var/audit/events.3.jsonl",
                        "blocked",
                        ["non_object_jsonl"],
                    ),
                    (
                        "var/audit/events.4.jsonl",
                        "blocked",
                        ["segment_unreadable"],
                    ),
                    (
                        "var/audit/events.5.jsonl",
                        "blocked",
                        ["unlink_failed"],
                    ),
                    (
                        "var/audit/events.6.jsonl",
                        "blocked",
                        ["metadata_unreadable"],
                    ),
                    (
                        "var/audit/events.7.jsonl",
                        "blocked",
                        ["invalid_utf8"],
                    ),
                    (
                        "var/audit/events.8.jsonl",
                        "blocked",
                        ["segment_disappeared"],
                    ),
                ],
            )
            self.assertIn(
                "segment bytes unavailable",
                counts["audit_segment_outcomes"][3]["error"],
            )
            self.assertIn(
                "segment held open",
                counts["audit_segment_outcomes"][4]["error"],
            )
            self.assertIn(
                "segment metadata unavailable",
                counts["audit_segment_outcomes"][5]["error"],
            )
            self.assertFalse(valid.exists())
            for path in (
                malformed,
                nonobject,
                unreadable,
                unlink_blocked,
                stat_blocked,
                invalid_utf8,
                disappeared,
            ):
                self.assertTrue(path.exists())

    def test_audit_prune_reports_global_degraded_snapshot_failure_per_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            first = old_file(audit_dir / "events.1.jsonl", "{}\n")
            second = old_file(audit_dir / "events.2.jsonl", "{}\n")
            (audit_dir / "degraded.jsonl").write_text("{bad}\n", encoding="utf-8")

            report = audit.prune_rotated(root, datetime.now(timezone.utc))

            self.assert_rotated_counts(report, 0, 0, 2)
            self.assertEqual(
                report["audit_segment_outcomes"],
                [
                    {
                        "path": "var/audit/events.1.jsonl",
                        "status": "blocked",
                        "reason_codes": ["degraded_snapshot_invalid"],
                    },
                    {
                        "path": "var/audit/events.2.jsonl",
                        "status": "blocked",
                        "reason_codes": ["degraded_snapshot_invalid"],
                    },
                ],
            )
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_oa_prune_reports_blocked_rotated_segments_without_counting_them_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            valid = old_file(audit_dir / "events.1.jsonl", "{}\n")
            malformed = old_file(audit_dir / "events.2.jsonl", "{bad}\n")

            _, report = self.run_module(
                "pao_runtime.oa_cli",
                "prune",
                "--older-than-days",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(report["audit_segments_removed"], 1)
            self.assertEqual(report["audit_segments_protected"], 0)
            self.assertEqual(report["audit_segments_blocked"], 1)
            self.assertEqual(report["total"], 1)
            self.assertFalse(report["audit_prune_audit_committed"])
            self.assertEqual(
                [
                    (outcome["path"], outcome["status"], outcome["reason_codes"])
                    for outcome in report["audit_segment_outcomes"]
                ],
                [
                    (
                        "var/audit/events.1.jsonl",
                        "removed",
                        ["valid_expired"],
                    ),
                    (
                        "var/audit/events.2.jsonl",
                        "blocked",
                        ["malformed_jsonl"],
                    ),
                ],
            )
            receipt_path = root / report["audit_prune_receipt"]
            self.assertTrue(receipt_path.is_file())
            degraded = audit_dir / "degraded.jsonl"
            self.assertTrue(degraded.is_file())
            degraded_events = [
                json.loads(line)
                for line in degraded.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                degraded_events[-1]["audit_prune_run_id"],
                report["audit_prune_run_id"],
            )

            digest = hashlib.sha256(malformed.read_bytes()).hexdigest()
            self.run_module(
                "pao_runtime.oa_cli",
                "audit-repair",
                "--segment",
                malformed.name,
                "--expected-sha256",
                digest,
                "--drop-line",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            _, resumed = self.run_module(
                "pao_runtime.oa_cli",
                "prune",
                "--older-than-days",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertTrue(resumed["audit_prune_resumed"])
            self.assertTrue(resumed["audit_prune_audit_committed"])
            self.assertEqual(
                resumed["audit_prune_run_id"],
                report["audit_prune_run_id"],
            )
            self.assertFalse(receipt_path.exists())
            events = [
                json.loads(line)
                for line in (audit_dir / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            pruned = [event for event in events if event["event"] == "pruned"][-1]
            self.assertEqual(
                pruned["audit_segment_outcomes"],
                report["audit_segment_outcomes"],
            )
            self.assertEqual(
                sum(
                    event.get("idempotency_key")
                    == report["audit_prune_audit_key"]
                    for event in events
                ),
                1,
            )
            self.assertFalse(valid.exists())
            self.assertTrue(malformed.is_file())

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
                report = future.result(timeout=2)
                self.assert_rotated_counts(report, 1, 0, 0)
                self.assertEqual(
                    report["audit_segment_outcomes"],
                    [
                        {
                            "path": "var/audit/events.1.jsonl",
                            "status": "removed",
                            "reason_codes": ["valid_expired"],
                        }
                    ],
                )
            self.assertFalse(rotated.exists())

    def test_rotated_prune_receipt_hard_crashes_converge_and_restore_audit_once(self):
        fault_code = (
            "import os,sys\n"
            "from datetime import datetime,timezone\n"
            "from pathlib import Path\n"
            "from pao_runtime import audit\n"
            "stop_after=int(sys.argv[2])\n"
            "real_unlink=Path.unlink\n"
            "seen=0\n"
            "def crash_unlink(self,*args,**kwargs):\n"
            " global seen\n"
            " result=real_unlink(self,*args,**kwargs)\n"
            " if self.parent.name=='audit' and self.name.startswith('events.') and self.name.endswith('.jsonl'):\n"
            "  seen+=1\n"
            "  if seen==stop_after:\n"
            "   os._exit(94+seen)\n"
            " return result\n"
            "Path.unlink=crash_unlink\n"
            "audit.prune_rotated(Path(sys.argv[1]),datetime.now(timezone.utc))\n"
        )
        for stop_after in (1, 2):
            with self.subTest(stop_after=stop_after), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                audit_dir = root / "var" / "audit"
                first = old_file(audit_dir / "events.1.jsonl", "{}\n")
                second = old_file(audit_dir / "events.2.jsonl", "{}\n")

                crashed = subprocess.run(
                    [sys.executable, "-c", fault_code, str(root), str(stop_after)],
                    cwd=REPO,
                    env={**os.environ, "PYTHONPATH": str(RUNTIME_HOME)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    crashed.returncode,
                    94 + stop_after,
                    crashed.stderr + crashed.stdout,
                )
                receipt_path = next((audit_dir / ".rotated-prune").glob("*.json"))
                prepared = audit._load_rotated_prune_receipt(receipt_path)
                self.assertEqual(prepared["phase"], "prepared")
                self.assertEqual(
                    sum(not path.exists() for path in (first, second)),
                    stop_after,
                )

                stale_time = time.time() - 31
                for lock in (audit_dir / ".audit.lock", audit_dir / ".degraded.lock"):
                    self.assertTrue(lock.is_file())
                    os.utime(lock, (stale_time, stale_time))
                report = audit.prune_rotated(
                    root, datetime.now(timezone.utc) - timedelta(days=100)
                )
                self.assertTrue(report["audit_prune_resumed"])
                self.assertEqual(report["audit_prune_run_id"], prepared["run_id"])
                self.assertEqual(report["audit_prune_cutoff"], prepared["cutoff"])
                self.assert_rotated_counts(report, 2, 0, 0)
                self.assertFalse(first.exists())
                self.assertFalse(second.exists())

                self.assertTrue(
                    audit.record_once(
                        root,
                        "oa",
                        {"event": "pruned", **report},
                        report["audit_prune_audit_key"],
                    )
                )
                self.assertTrue(audit.commit_rotated_prune_receipt(root, report))
                self.assertFalse(receipt_path.exists())
                events = [
                    json.loads(line)
                    for line in (audit_dir / "events.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.strip()
                ]
                self.assertEqual(
                    sum(
                        event.get("idempotency_key")
                        == report["audit_prune_audit_key"]
                        for event in events
                    ),
                    1,
                )

    def test_rotated_prune_receipt_fails_closed_on_drift_and_requires_audit_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_dir = root / "var" / "audit"
            target = old_file(audit_dir / "events.1.jsonl", "{}\n")

            with mock.patch.object(
                audit,
                "_apply_rotated_prune_receipt",
                side_effect=OSError("stop after durable prepare"),
            ):
                with self.assertRaisesRegex(OSError, "durable prepare"):
                    audit.prune_rotated(root, datetime.now(timezone.utc))
            receipt_path = next((audit_dir / ".rotated-prune").glob("*.json"))
            prepared = audit._load_rotated_prune_receipt(receipt_path)
            self.assertEqual(prepared["phase"], "prepared")
            target.write_text('{"event":"drifted"}\n', encoding="utf-8")

            report = audit.prune_rotated(root, datetime.now(timezone.utc))

            self.assertTrue(report["audit_prune_resumed"])
            self.assert_rotated_counts(report, 0, 0, 1)
            self.assertEqual(
                report["audit_segment_outcomes"][0]["reason_codes"],
                ["segment_drifted"],
            )
            self.assertTrue(target.is_file())
            with self.assertRaisesRegex(OSError, "not committed"):
                audit.commit_rotated_prune_receipt(root, report)
            self.assertTrue(receipt_path.is_file())
            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {"event": "pruned", **report},
                    report["audit_prune_audit_key"],
                )
            )
            self.assertTrue(audit.commit_rotated_prune_receipt(root, report))
            self.assertFalse(receipt_path.exists())

    def test_audit_health_classifies_rotated_prune_crash_states_read_only(self):
        cases = (
            "prepared_matching",
            "prepared_authorized_absent",
            "applied_audit_missing",
            "applied_audit_present",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                audit_dir = root / "var" / "audit"
                target = old_file(audit_dir / "events.1.jsonl", "{}\n")
                with mock.patch.object(
                    audit,
                    "_apply_rotated_prune_receipt",
                    side_effect=OSError("stop after durable prepare"),
                ):
                    with self.assertRaisesRegex(OSError, "durable prepare"):
                        audit.prune_rotated(root, datetime.now(timezone.utc))
                receipt_path = next((audit_dir / ".rotated-prune").glob("*.json"))
                prepared = audit._load_rotated_prune_receipt(receipt_path)
                expected_phase = "prepared"
                expected_target_state = "matching"
                expected_key_present = False

                if case == "prepared_authorized_absent":
                    target.unlink()
                    expected_target_state = "authorized_absent"
                elif case in {"applied_audit_missing", "applied_audit_present"}:
                    applied = audit.prune_rotated(root, datetime.now(timezone.utc))
                    expected_phase = "applied"
                    expected_target_state = "authorized_absent"
                    if case == "applied_audit_present":
                        self.assertTrue(
                            audit.record_once(
                                root,
                                "oa",
                                {"event": "pruned", **applied},
                                applied["audit_prune_audit_key"],
                            )
                        )
                        expected_key_present = True

                before_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                before_dirs = {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                }
                _, report = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-health",
                    "--root",
                    str(root),
                    expected=0,
                )
                after_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                after_dirs = {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                }

                self.assertEqual(after_files, before_files)
                self.assertEqual(after_dirs, before_dirs)
                self.assertEqual(report["status"], "attention")
                self.assertEqual(report["resumable_rotated_prune_count"], 1)
                self.assertEqual(report["blocked_rotated_prune_count"], 0)
                health = report["rotated_prune_receipts"][0]
                self.assertEqual(health["status"], "resumable")
                self.assertEqual(health["reason_codes"], [])
                self.assertEqual(health["phase"], expected_phase)
                self.assertEqual(health["run_id"], prepared["run_id"])
                self.assertEqual(
                    health["audit_key_present"], expected_key_present
                )
                self.assertEqual(
                    health["removal_target_states"],
                    [
                        {
                            "path": "var/audit/events.1.jsonl",
                            "state": expected_target_state,
                        }
                    ],
                )
                self.assertTrue(
                    any(
                        "Run prune" in item and "rotated-prune" in item
                        for item in report["guidance"]
                    )
                )

    def test_audit_health_reason_codes_blocked_rotated_prune_states(self):
        cases = {
            "invalid_receipt": (1, "invalid_receipt"),
            "multiple_receipts": (2, "multiple_pending_receipts"),
            "prepared_drift": (1, "segment_drifted"),
            "applied_target_present": (1, "applied_target_present"),
            "target_not_file": (1, "target_not_file"),
            "unexpected_entry": (1, "unexpected_entry"),
            "receipt_directory_not_directory": (
                1,
                "receipt_directory_not_directory",
            ),
        }
        for case, (expected_count, expected_reason) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                audit_dir = root / "var" / "audit"
                target = old_file(audit_dir / "events.1.jsonl", "{}\n")
                with mock.patch.object(
                    audit,
                    "_apply_rotated_prune_receipt",
                    side_effect=OSError("stop after durable prepare"),
                ):
                    with self.assertRaisesRegex(OSError, "durable prepare"):
                        audit.prune_rotated(root, datetime.now(timezone.utc))
                receipt_dir = audit_dir / ".rotated-prune"
                receipt_path = next(receipt_dir.glob("*.json"))

                if case == "invalid_receipt":
                    receipt_path.write_text("{invalid}\n", encoding="utf-8")
                elif case == "multiple_receipts":
                    duplicate = receipt_dir / f"{'0' * 64}.json"
                    duplicate.write_bytes(receipt_path.read_bytes())
                elif case == "prepared_drift":
                    target.write_text('{"event":"drifted"}\n', encoding="utf-8")
                elif case == "applied_target_present":
                    audit.prune_rotated(root, datetime.now(timezone.utc))
                    target.write_text("{}\n", encoding="utf-8")
                elif case == "target_not_file":
                    target.unlink()
                    target.mkdir()
                elif case == "unexpected_entry":
                    receipt_path.unlink()
                    (receipt_dir / "unexpected.txt").write_text(
                        "unexpected\n", encoding="utf-8"
                    )
                elif case == "receipt_directory_not_directory":
                    receipt_path.unlink()
                    receipt_dir.rmdir()
                    receipt_dir.write_text("not a directory\n", encoding="utf-8")

                before_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                before_dirs = {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                }
                health = audit.health(root)
                after_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                after_dirs = {
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                }

                self.assertEqual(after_files, before_files)
                self.assertEqual(after_dirs, before_dirs)
                self.assertEqual(health["status"], "attention")
                self.assertEqual(health["resumable_rotated_prune_count"], 0)
                self.assertEqual(
                    health["blocked_rotated_prune_count"], expected_count
                )
                self.assertTrue(
                    any(
                        expected_reason in item["reason_codes"]
                        for item in health["rotated_prune_receipts"]
                    )
                )
                self.assertTrue(
                    any(
                        "Do not delete blocked rotated-prune" in item
                        for item in health["guidance"]
                    )
                )
                if case == "applied_target_present":
                    with self.assertRaisesRegex(
                        ValueError, "applied rotated prune target is present"
                    ):
                        audit.prune_rotated(root, datetime.now(timezone.utc))

    def test_audit_prune_resolve_preserves_recreated_target_and_fences_future_prune(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                target,
                receipt_path,
                receipt_sha256,
                segment_sha256,
                pruned,
            ) = self._create_applied_recreated_prune(root)
            recreated = target.read_bytes()

            _, resolved = self.run_module(
                "pao_runtime.oa_cli",
                "audit-prune-resolve",
                "--run-id",
                pruned["audit_prune_run_id"],
                "--expected-receipt-sha256",
                receipt_sha256,
                "--segment",
                target.name,
                "--expected-segment-sha256",
                segment_sha256,
                "--decision",
                "preserve-recreated",
                "--root",
                str(root),
                expected=0,
            )

            self.assertFalse(resolved["already_resolved"])
            self.assertTrue(resolved["pruned_event_committed"])
            self.assertTrue(resolved["resolution_event_committed"])
            self.assertTrue(resolved["receipt_completed"])
            self.assertFalse(receipt_path.exists())
            self.assertEqual(target.read_bytes(), recreated)
            marker_path = target.parent / resolved["preservation"]
            self.assertTrue(marker_path.is_file())
            marker = audit._load_rotated_preservation(marker_path)
            self.assertEqual(marker["preserved_sha256"], segment_sha256)
            self.assertEqual(marker["decision"], "preserve-recreated")

            events = [
                json.loads(line)
                for line in (target.parent / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key")
                    == pruned["audit_prune_audit_key"]
                    for event in events
                ),
                1,
            )
            self.assertEqual(
                sum(
                    event.get("idempotency_key")
                    == resolved["resolution_audit_key"]
                    for event in events
                ),
                1,
            )

            _, future = self.run_module(
                "pao_runtime.oa_cli",
                "prune",
                "--older-than-days",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(target.read_bytes(), recreated)
            self.assertEqual(future["audit_segments_removed"], 0)
            self.assertEqual(future["audit_segments_protected"], 1)
            self.assertEqual(
                future["audit_segment_outcomes"][0]["reason_codes"],
                ["operator_preserved_target"],
            )

    def test_audit_health_reports_protected_rotated_preservation_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, marker_path, resolved = self._create_resolved_preservation(root)
            before_files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            before_dirs = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir()
            )

            _, health = self.run_module(
                "pao_runtime.oa_cli",
                "audit-health",
                "--root",
                str(root),
                expected=0,
            )

            after_files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            after_dirs = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir()
            )
            self.assertEqual(after_files, before_files)
            self.assertEqual(after_dirs, before_dirs)
            self.assertEqual(health["status"], "attention")
            self.assertFalse(health["keyed_append_blocked"])
            self.assertEqual(health["protected_rotated_preservation_count"], 1)
            self.assertEqual(health["blocked_rotated_preservation_count"], 0)
            self.assertEqual(len(health["rotated_preservations"]), 1)
            preservation = health["rotated_preservations"][0]
            self.assertEqual(
                preservation["path"],
                marker_path.relative_to(target.parent).as_posix(),
            )
            self.assertEqual(preservation["status"], "protected")
            self.assertEqual(preservation["reason_codes"], [])
            self.assertEqual(preservation["target_state"], "matching")
            self.assertTrue(preservation["pruned_audit_key_present"])
            self.assertTrue(preservation["resolution_audit_key_present"])
            self.assertEqual(
                preservation["resolution_audit_key"],
                resolved["resolution_audit_key"],
            )
            self.assertTrue(
                any(
                    "Retain protected rotated-preservation" in item
                    for item in health["guidance"]
                )
            )

    def test_audit_health_reason_codes_blocked_rotated_preservations(self):
        cases = (
            "orphaned_marker",
            "target_fingerprint_drift",
            "resolution_audit_missing",
            "duplicate_target_claim",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, marker_path, resolved = self._create_resolved_preservation(root)
                marker = audit._load_rotated_preservation(marker_path)
                if case == "orphaned_marker":
                    target.unlink()
                elif case == "target_fingerprint_drift":
                    target.write_bytes(b'{"event":"drifted"}\n')
                elif case == "resolution_audit_missing":
                    events_path = target.parent / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    events_path.write_text(
                        "".join(
                            json.dumps(event, sort_keys=True) + "\n"
                            for event in events
                            if event.get("idempotency_key")
                            != resolved["resolution_audit_key"]
                        ),
                        encoding="utf-8",
                    )
                else:
                    duplicate_run_id = (
                        "0" * 64 if marker["run_id"] != "0" * 64 else "1" * 64
                    )
                    duplicate_key = (
                        f"rotated-prune-resolve:{duplicate_run_id}:"
                        f"{marker['segment']}:{marker['preserved_sha256']}"
                    )
                    duplicate = {
                        **marker,
                        "run_id": duplicate_run_id,
                        "audit_key": duplicate_key,
                    }
                    duplicate_path = marker_path.with_name(
                        f"{duplicate_run_id}.{marker['segment']}.json"
                    )
                    duplicate_path.write_text(
                        json.dumps(duplicate, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    self.assertTrue(
                        audit.record_once(
                            root,
                            "oa",
                            {"event": "duplicate_preservation_prune_witness"},
                            f"rotated-prune:{duplicate_run_id}",
                        )
                    )
                    self.assertTrue(
                        audit.record_once(
                            root,
                            "oa",
                            {"event": "duplicate_preservation_resolution_witness"},
                            duplicate_key,
                        )
                    )

                before_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                before_dirs = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                )
                _, health = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-health",
                    "--root",
                    str(root),
                    expected=0,
                )
                after_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                after_dirs = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                )
                self.assertEqual(after_files, before_files)
                self.assertEqual(after_dirs, before_dirs)
                self.assertEqual(health["status"], "attention")
                self.assertFalse(health["keyed_append_blocked"])
                self.assertEqual(health["protected_rotated_preservation_count"], 0)
                expected_count = 2 if case == "duplicate_target_claim" else 1
                self.assertEqual(
                    health["blocked_rotated_preservation_count"],
                    expected_count,
                )
                self.assertTrue(
                    any(
                        case in item["reason_codes"]
                        for item in health["rotated_preservations"]
                    )
                )
                self.assertTrue(
                    any(
                        "Do not delete blocked rotated-preservation" in item
                        for item in health["guidance"]
                    )
                )

    def test_audit_health_classifies_preservation_release_topology_read_only(self):
        for topology in ("event_first", "completed"):
            with self.subTest(topology=topology), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, marker_path, _ = self._create_resolved_preservation(root)
                _, prepared = self._create_event_first_preservation_release(
                    root, target, marker_path
                )
                if topology == "completed":
                    self.assertTrue(
                        audit.commit_rotated_preservation_release(root, prepared)
                    )
                    self.assertFalse(marker_path.exists())
                before_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                before_dirs = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                )

                _, health = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-health",
                    "--root",
                    str(root),
                    expected=0,
                )

                after_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                after_dirs = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                )
                self.assertEqual(after_files, before_files)
                self.assertEqual(after_dirs, before_dirs)
                self.assertFalse(health["keyed_append_blocked"])
                self.assertEqual(len(health["preservation_releases"]), 1)
                release = health["preservation_releases"][0]
                self.assertEqual(
                    release["release_audit_key"],
                    prepared["release_audit_key"],
                )
                self.assertEqual(release["event_count"], 1)
                self.assertEqual(
                    release["marker_state"],
                    "present" if topology == "event_first" else "absent",
                )
                if topology == "event_first":
                    self.assertEqual(health["status"], "attention")
                    self.assertEqual(
                        health["resumable_preservation_release_count"], 1
                    )
                    self.assertEqual(
                        health["completed_preservation_release_count"], 0
                    )
                    self.assertEqual(
                        health["blocked_preservation_release_count"], 0
                    )
                    self.assertEqual(release["status"], "resumable")
                    self.assertEqual(
                        release["reason_codes"],
                        ["release_event_committed_marker_present"],
                    )
                    self.assertTrue(
                        any(
                            "Retry audit-preserve-release" in item
                            for item in health["guidance"]
                        )
                    )
                else:
                    self.assertEqual(health["status"], "healthy")
                    self.assertEqual(
                        health["completed_preservation_release_count"], 1
                    )
                    self.assertEqual(
                        health["resumable_preservation_release_count"], 0
                    )
                    self.assertEqual(
                        health["blocked_preservation_release_count"], 0
                    )
                    self.assertEqual(release["status"], "completed")
                    self.assertEqual(release["reason_codes"], [])
                    self.assertEqual(health["guidance"], ["No action required."])

    def test_audit_health_blocks_duplicate_and_conflicting_release_events(self):
        for case in ("duplicate_release_event", "release_event_payload_conflict"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, marker_path, _ = self._create_resolved_preservation(root)
                _, prepared = self._create_event_first_preservation_release(
                    root, target, marker_path
                )
                events_path = target.parent / "events.jsonl"
                events = [
                    json.loads(line)
                    for line in events_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                release_event = next(
                    event
                    for event in events
                    if event.get("idempotency_key")
                    == prepared["release_audit_key"]
                )
                duplicate = {**release_event, "ts": utc_now()}
                if case == "release_event_payload_conflict":
                    duplicate["decision"] = "conflicting-decision"
                with events_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(duplicate, sort_keys=True) + "\n")
                before_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                before_dirs = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                )

                _, health = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-health",
                    "--root",
                    str(root),
                    expected=0,
                )

                after_files = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                after_dirs = sorted(
                    path.relative_to(root).as_posix()
                    for path in root.rglob("*")
                    if path.is_dir()
                )
                self.assertEqual(after_files, before_files)
                self.assertEqual(after_dirs, before_dirs)
                self.assertEqual(health["status"], "attention")
                self.assertFalse(health["keyed_append_blocked"])
                self.assertEqual(
                    health["blocked_preservation_release_count"], 1
                )
                release = health["preservation_releases"][0]
                self.assertEqual(release["status"], "blocked")
                self.assertEqual(release["event_count"], 2)
                self.assertIn("duplicate_release_event", release["reason_codes"])
                if case == "release_event_payload_conflict":
                    self.assertIn(
                        "release_event_payload_conflict",
                        release["reason_codes"],
                    )
                else:
                    self.assertNotIn(
                        "release_event_payload_conflict",
                        release["reason_codes"],
                    )
                self.assertTrue(
                    any(
                        "Do not remove release markers" in item
                        for item in health["guidance"]
                    )
                )

    def test_audit_health_blocks_release_marker_drift_and_binding_failure(self):
        for case in ("release_marker_fingerprint_drift", "release_marker_binding_blocked"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, marker_path, resolved = self._create_resolved_preservation(root)
                _, prepared = self._create_event_first_preservation_release(
                    root, target, marker_path
                )
                if case == "release_marker_fingerprint_drift":
                    marker_path.write_bytes(marker_path.read_bytes() + b" ")
                else:
                    events_path = target.parent / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    events_path.write_text(
                        "".join(
                            json.dumps(event, sort_keys=True) + "\n"
                            for event in events
                            if event.get("idempotency_key")
                            != resolved["resolution_audit_key"]
                        ),
                        encoding="utf-8",
                    )

                _, health = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-health",
                    "--root",
                    str(root),
                    expected=0,
                )

                self.assertEqual(health["status"], "attention")
                self.assertFalse(health["keyed_append_blocked"])
                self.assertEqual(
                    health["blocked_preservation_release_count"], 1
                )
                release = next(
                    item
                    for item in health["preservation_releases"]
                    if item.get("release_audit_key")
                    == prepared["release_audit_key"]
                )
                self.assertEqual(release["status"], "blocked")
                self.assertIn(case, release["reason_codes"])

    def test_audit_preserve_release_hard_crash_is_discovered_and_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, marker_path, _ = self._create_resolved_preservation(root)
            fence = self._preservation_release_fence(target, marker_path)
            target_before = target.read_bytes()
            fault_code = (
                "import os,sys\n"
                "from pao_runtime import audit,oa_cli\n"
                "def crash_before_marker_unlink(*args,**kwargs):\n"
                " os._exit(97)\n"
                "audit.commit_rotated_preservation_release="
                "crash_before_marker_unlink\n"
                "sys.argv=['oa','audit-preserve-release',"
                "'--run-id',sys.argv[2],'--segment',sys.argv[3],"
                "'--expected-marker-sha256',sys.argv[4],"
                "'--expected-segment-sha256',sys.argv[5],"
                "'--decision','release-protection','--root',sys.argv[1]]\n"
                "raise SystemExit(oa_cli.main())\n"
            )
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    fault_code,
                    str(root),
                    fence["run_id"],
                    fence["segment"],
                    fence["marker_sha256"],
                    fence["segment_sha256"],
                ],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PAO_OA_ID": "oa-test",
                    "PYTHONPATH": str(RUNTIME_HOME),
                },
                check=False,
            )
            self.assertEqual(
                crashed.returncode,
                97,
                crashed.stderr + crashed.stdout,
            )
            self.assertTrue(marker_path.is_file())
            self.assertEqual(target.read_bytes(), target_before)
            events_path = target.parent / "events.jsonl"
            events_after_crash = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            release_key = audit._preservation_release_audit_key(
                fence["run_id"],
                fence["segment"],
                fence["marker_sha256"],
                fence["segment_sha256"],
            )
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == release_key
                    for event in events_after_crash
                ),
                1,
            )
            command_lock = root / "var" / "oa" / ".command.lock"
            self.assertTrue(command_lock.is_file())
            self.assertFalse((target.parent / ".audit.lock").exists())
            self.assertFalse((target.parent / ".degraded.lock").exists())

            before_health_files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            before_health_dirs = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir()
            )
            _, health = self.run_module(
                "pao_runtime.oa_cli",
                "audit-health",
                "--root",
                str(root),
                expected=0,
            )
            after_health_files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            after_health_dirs = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir()
            )
            self.assertEqual(after_health_files, before_health_files)
            self.assertEqual(after_health_dirs, before_health_dirs)
            self.assertEqual(health["status"], "attention")
            self.assertEqual(health["resumable_preservation_release_count"], 1)
            resumable = health["preservation_releases"][0]
            self.assertEqual(resumable["status"], "resumable")
            self.assertEqual(resumable["release_audit_key"], release_key)
            self.assertEqual(resumable["run_id"], fence["run_id"])
            self.assertEqual(resumable["segment"], fence["segment"])
            self.assertEqual(
                resumable["marker_sha256"],
                fence["marker_sha256"],
            )
            self.assertEqual(
                resumable["preserved_sha256"],
                fence["segment_sha256"],
            )

            stale_stamp = time.time() - 60
            os.utime(command_lock, (stale_stamp, stale_stamp))
            _, recovered = self.run_module(
                "pao_runtime.oa_cli",
                "audit-preserve-release",
                "--run-id",
                resumable["run_id"],
                "--segment",
                resumable["segment"],
                "--expected-marker-sha256",
                resumable["marker_sha256"],
                "--expected-segment-sha256",
                resumable["preserved_sha256"],
                "--decision",
                resumable["decision"],
                "--root",
                str(root),
                expected=0,
            )
            self.assertTrue(recovered["release_completed"])
            self.assertTrue(recovered["marker_removed"])
            self.assertTrue(recovered["target_preserved"])
            self.assertFalse(marker_path.exists())
            self.assertEqual(target.read_bytes(), target_before)
            self.assertFalse(command_lock.exists())
            recovered_events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == release_key
                    for event in recovered_events
                ),
                1,
            )
            _, final_health = self.run_module(
                "pao_runtime.oa_cli",
                "audit-health",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(final_health["status"], "healthy")
            self.assertEqual(
                final_health["completed_preservation_release_count"], 1
            )
            self.assertEqual(
                final_health["resumable_preservation_release_count"], 0
            )

    def test_audit_preserve_release_post_unlink_crash_is_completed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, marker_path, _ = self._create_resolved_preservation(root)
            fence = self._preservation_release_fence(target, marker_path)
            target_before = target.read_bytes()
            fault_code = (
                "import os,sys\n"
                "from pao_runtime import audit,oa_cli\n"
                "real_commit=audit.commit_rotated_preservation_release\n"
                "def crash_after_marker_unlink(*args,**kwargs):\n"
                " result=real_commit(*args,**kwargs)\n"
                " if result is not True:\n"
                "  os._exit(96)\n"
                " os._exit(98)\n"
                "audit.commit_rotated_preservation_release="
                "crash_after_marker_unlink\n"
                "sys.argv=['oa','audit-preserve-release',"
                "'--run-id',sys.argv[2],'--segment',sys.argv[3],"
                "'--expected-marker-sha256',sys.argv[4],"
                "'--expected-segment-sha256',sys.argv[5],"
                "'--decision','release-protection','--root',sys.argv[1]]\n"
                "raise SystemExit(oa_cli.main())\n"
            )
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    fault_code,
                    str(root),
                    fence["run_id"],
                    fence["segment"],
                    fence["marker_sha256"],
                    fence["segment_sha256"],
                ],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PAO_OA_ID": "oa-test",
                    "PYTHONPATH": str(RUNTIME_HOME),
                },
                check=False,
            )
            self.assertEqual(
                crashed.returncode,
                98,
                crashed.stderr + crashed.stdout,
            )
            self.assertFalse(marker_path.exists())
            self.assertEqual(target.read_bytes(), target_before)
            release_key = audit._preservation_release_audit_key(
                fence["run_id"],
                fence["segment"],
                fence["marker_sha256"],
                fence["segment_sha256"],
            )
            events_path = target.parent / "events.jsonl"
            events_after_crash = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == release_key
                    for event in events_after_crash
                ),
                1,
            )
            command_lock = root / "var" / "oa" / ".command.lock"
            self.assertTrue(command_lock.is_file())
            self.assertFalse((target.parent / ".audit.lock").exists())
            self.assertFalse((target.parent / ".degraded.lock").exists())

            before_health_files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            before_health_dirs = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir()
            )
            _, health = self.run_module(
                "pao_runtime.oa_cli",
                "audit-health",
                "--root",
                str(root),
                expected=0,
            )
            after_health_files = {
                path.relative_to(root).as_posix(): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            after_health_dirs = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_dir()
            )
            self.assertEqual(after_health_files, before_health_files)
            self.assertEqual(after_health_dirs, before_health_dirs)
            self.assertEqual(health["status"], "healthy")
            self.assertEqual(health["completed_preservation_release_count"], 1)
            self.assertEqual(health["resumable_preservation_release_count"], 0)
            completed = health["preservation_releases"][0]
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["marker_state"], "absent")
            self.assertEqual(completed["release_audit_key"], release_key)

            stale_stamp = time.time() - 60
            os.utime(command_lock, (stale_stamp, stale_stamp))
            _, retried = self.run_module(
                "pao_runtime.oa_cli",
                "audit-preserve-release",
                "--run-id",
                completed["run_id"],
                "--segment",
                completed["segment"],
                "--expected-marker-sha256",
                completed["marker_sha256"],
                "--expected-segment-sha256",
                completed["preserved_sha256"],
                "--decision",
                completed["decision"],
                "--root",
                str(root),
                expected=0,
            )
            self.assertTrue(retried["already_released"])
            self.assertFalse(retried["marker_removed"])
            self.assertTrue(retried["release_completed"])
            self.assertTrue(retried["target_preserved"])
            self.assertFalse(marker_path.exists())
            self.assertEqual(target.read_bytes(), target_before)
            self.assertFalse(command_lock.exists())
            events_after_retry = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key") == release_key
                    for event in events_after_retry
                ),
                1,
            )

    def test_audit_preserve_release_keeps_target_and_enables_later_prune(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, marker_path, _ = self._create_resolved_preservation(root)
            fence = self._preservation_release_fence(target, marker_path)
            target_before = target.read_bytes()

            _, released = self.run_module(
                "pao_runtime.oa_cli",
                "audit-preserve-release",
                "--run-id",
                fence["run_id"],
                "--segment",
                fence["segment"],
                "--expected-marker-sha256",
                fence["marker_sha256"],
                "--expected-segment-sha256",
                fence["segment_sha256"],
                "--decision",
                fence["decision"],
                "--root",
                str(root),
                expected=0,
            )

            self.assertEqual(released["event"], "audit_preservation_release")
            self.assertFalse(released["already_released"])
            self.assertTrue(released["release_event_committed"])
            self.assertTrue(released["marker_removed"])
            self.assertTrue(released["release_completed"])
            self.assertTrue(released["target_preserved"])
            self.assertFalse(marker_path.exists())
            self.assertEqual(target.read_bytes(), target_before)
            self.assertEqual(released["health_status"], "healthy")
            events = [
                json.loads(line)
                for line in (target.parent / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            release_events = [
                event
                for event in events
                if event.get("idempotency_key") == released["release_audit_key"]
            ]
            self.assertEqual(len(release_events), 1)
            self.assertEqual(
                release_events[0]["event"],
                "audit_preservation_released",
            )
            self.assertEqual(
                release_events[0]["marker_sha256"],
                fence["marker_sha256"],
            )

            _, pruned = self.run_module(
                "pao_runtime.oa_cli",
                "prune",
                "--older-than-days",
                "1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertFalse(target.exists())
            self.assertEqual(pruned["audit_segments_removed"], 1)
            self.assertEqual(
                pruned["audit_segment_outcomes"][0]["reason_codes"],
                ["valid_expired"],
            )

    def test_audit_preserve_release_exact_retry_converges_across_event_and_unlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, marker_path, _ = self._create_resolved_preservation(root)
            fence = self._preservation_release_fence(target, marker_path)
            target_before = target.read_bytes()
            prepared = audit.prepare_rotated_preservation_release(
                root,
                run_id=fence["run_id"],
                segment=fence["segment"],
                expected_marker_sha256=fence["marker_sha256"],
                expected_segment_sha256=fence["segment_sha256"],
                decision=fence["decision"],
            )
            self.assertTrue(
                audit.record_once(
                    root,
                    "oa",
                    {
                        "event": "audit_preservation_released",
                        "decision": prepared["decision"],
                        "run_id": prepared["run_id"],
                        "segment": prepared["segment"],
                        "preservation": prepared["preservation"],
                        "marker_sha256": prepared["marker_sha256"],
                        "preserved_sha256": prepared["preserved_sha256"],
                        "preserved_bytes": prepared["preserved_bytes"],
                        "pruned_audit_key": prepared["pruned_audit_key"],
                        "resolution_audit_key": prepared[
                            "resolution_audit_key"
                        ],
                    },
                    prepared["release_audit_key"],
                )
            )
            self.assertTrue(marker_path.is_file())

            command = (
                "audit-preserve-release",
                "--run-id",
                fence["run_id"],
                "--segment",
                fence["segment"],
                "--expected-marker-sha256",
                fence["marker_sha256"],
                "--expected-segment-sha256",
                fence["segment_sha256"],
                "--decision",
                fence["decision"],
                "--root",
                str(root),
            )
            _, event_first_retry = self.run_module(
                "pao_runtime.oa_cli",
                *command,
                expected=0,
            )
            self.assertFalse(event_first_retry["already_released"])
            self.assertTrue(event_first_retry["marker_removed"])
            self.assertFalse(marker_path.exists())
            self.assertEqual(target.read_bytes(), target_before)

            _, post_unlink_retry = self.run_module(
                "pao_runtime.oa_cli",
                *command,
                expected=0,
            )
            self.assertTrue(post_unlink_retry["already_released"])
            self.assertFalse(post_unlink_retry["marker_removed"])
            self.assertTrue(post_unlink_retry["release_completed"])
            self.assertTrue(post_unlink_retry["target_preserved"])
            self.assertEqual(target.read_bytes(), target_before)
            events = [
                json.loads(line)
                for line in (target.parent / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                sum(
                    event.get("idempotency_key")
                    == prepared["release_audit_key"]
                    for event in events
                ),
                1,
            )

    def test_audit_preserve_release_refuses_fence_and_binding_ambiguity(self):
        cases = (
            "marker_hash",
            "segment_hash",
            "run_id",
            "segment",
            "marker_drift",
            "target_drift",
            "resolution_audit_missing",
            "duplicate_target_claim",
            "release_event_collision",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, marker_path, resolved = self._create_resolved_preservation(root)
                fence = self._preservation_release_fence(target, marker_path)
                run_id = fence["run_id"]
                segment = fence["segment"]
                marker_sha256 = fence["marker_sha256"]
                segment_sha256 = fence["segment_sha256"]
                if case == "marker_hash":
                    marker_sha256 = "0" * 64
                elif case == "segment_hash":
                    segment_sha256 = "0" * 64
                elif case == "run_id":
                    run_id = "0" * 64 if run_id != "0" * 64 else "1" * 64
                elif case == "segment":
                    segment = "events.2.jsonl"
                elif case == "marker_drift":
                    marker_path.write_bytes(marker_path.read_bytes() + b" ")
                elif case == "target_drift":
                    target.write_bytes(b'{"event":"release-drift"}\n')
                elif case == "resolution_audit_missing":
                    events_path = target.parent / "events.jsonl"
                    events = [
                        json.loads(line)
                        for line in events_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    events_path.write_text(
                        "".join(
                            json.dumps(event, sort_keys=True) + "\n"
                            for event in events
                            if event.get("idempotency_key")
                            != resolved["resolution_audit_key"]
                        ),
                        encoding="utf-8",
                    )
                elif case == "duplicate_target_claim":
                    marker = audit._load_rotated_preservation(marker_path)
                    duplicate_run_id = (
                        "0" * 64 if marker["run_id"] != "0" * 64 else "1" * 64
                    )
                    duplicate_key = (
                        f"rotated-prune-resolve:{duplicate_run_id}:"
                        f"{marker['segment']}:{marker['preserved_sha256']}"
                    )
                    duplicate = {
                        **marker,
                        "run_id": duplicate_run_id,
                        "audit_key": duplicate_key,
                    }
                    duplicate_path = marker_path.with_name(
                        f"{duplicate_run_id}.{marker['segment']}.json"
                    )
                    duplicate_path.write_text(
                        json.dumps(duplicate, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                    self.assertTrue(
                        audit.record_once(
                            root,
                            "oa",
                            {"event": "duplicate_release_prune_witness"},
                            f"rotated-prune:{duplicate_run_id}",
                        )
                    )
                    self.assertTrue(
                        audit.record_once(
                            root,
                            "oa",
                            {"event": "duplicate_release_resolution_witness"},
                            duplicate_key,
                        )
                    )
                elif case == "release_event_collision":
                    release_key = audit._preservation_release_audit_key(
                        run_id,
                        segment,
                        marker_sha256,
                        segment_sha256,
                    )
                    self.assertTrue(
                        audit.record_once(
                            root,
                            "oa",
                            {"event": "colliding_release_event"},
                            release_key,
                        )
                    )

                audit_dir = root / "var" / "audit"
                before = {
                    path.relative_to(audit_dir).as_posix(): path.read_bytes()
                    for path in audit_dir.rglob("*")
                    if path.is_file()
                }
                completed, _ = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-preserve-release",
                    "--run-id",
                    run_id,
                    "--segment",
                    segment,
                    "--expected-marker-sha256",
                    marker_sha256,
                    "--expected-segment-sha256",
                    segment_sha256,
                    "--decision",
                    fence["decision"],
                    "--root",
                    str(root),
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("audit preservation release", completed.stderr)
                after = {
                    path.relative_to(audit_dir).as_posix(): path.read_bytes()
                    for path in audit_dir.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertTrue(marker_path.is_file())
                self.assertTrue(target.is_file())

    def test_audit_preserve_release_requires_the_oa_writer_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, marker_path, _ = self._create_resolved_preservation(root)
            fence = self._preservation_release_fence(target, marker_path)
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "audit-preserve-release",
                "--run-id",
                fence["run_id"],
                "--segment",
                fence["segment"],
                "--expected-marker-sha256",
                fence["marker_sha256"],
                "--expected-segment-sha256",
                fence["segment_sha256"],
                "--decision",
                fence["decision"],
                "--root",
                str(root),
                env={"PAO_OA_ID": ""},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("PAO_OA_ID is required", completed.stderr)
            self.assertTrue(marker_path.is_file())
            self.assertTrue(target.is_file())

    def test_audit_prune_resolve_exact_retry_recovers_after_receipt_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                target,
                receipt_path,
                receipt_sha256,
                segment_sha256,
                pruned,
            ) = self._create_applied_recreated_prune(root)

            interrupted = audit.resolve_rotated_prune(
                root,
                run_id=pruned["audit_prune_run_id"],
                expected_receipt_sha256=receipt_sha256,
                segment=target.name,
                expected_segment_sha256=segment_sha256,
                decision="preserve-recreated",
            )
            self.assertFalse(interrupted["already_resolved"])
            self.assertTrue(receipt_path.is_file())

            _, resumed = self.run_module(
                "pao_runtime.oa_cli",
                "audit-prune-resolve",
                "--run-id",
                pruned["audit_prune_run_id"],
                "--expected-receipt-sha256",
                receipt_sha256,
                "--segment",
                target.name,
                "--expected-segment-sha256",
                segment_sha256,
                "--decision",
                "preserve-recreated",
                "--root",
                str(root),
                expected=0,
            )

            self.assertTrue(resumed["already_resolved"])
            self.assertTrue(resumed["receipt_completed"])
            self.assertFalse(receipt_path.exists())
            self.assertTrue(target.is_file())
            events = [
                json.loads(line)
                for line in (target.parent / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            for key in (
                pruned["audit_prune_audit_key"],
                interrupted["resolution_audit_key"],
            ):
                self.assertEqual(
                    sum(event.get("idempotency_key") == key for event in events),
                    1,
                )

    def test_audit_prune_resolve_recovers_marker_first_crash_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                target,
                receipt_path,
                receipt_sha256,
                segment_sha256,
                pruned,
            ) = self._create_applied_recreated_prune(root)
            real_atomic_write = audit.atomic_write_json

            def stop_after_marker(path, payload):
                real_atomic_write(path, payload)
                if path.parent.name == ".rotated-preserve":
                    raise OSError("stop after preservation marker")

            with mock.patch.object(
                audit, "atomic_write_json", side_effect=stop_after_marker
            ):
                with self.assertRaisesRegex(OSError, "preservation marker"):
                    audit.resolve_rotated_prune(
                        root,
                        run_id=pruned["audit_prune_run_id"],
                        expected_receipt_sha256=receipt_sha256,
                        segment=target.name,
                        expected_segment_sha256=segment_sha256,
                        decision="preserve-recreated",
                    )
            self.assertEqual(
                hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
                receipt_sha256,
            )
            marker_path = next((target.parent / ".rotated-preserve").glob("*.json"))
            marker_before = marker_path.read_bytes()

            resumed = audit.resolve_rotated_prune(
                root,
                run_id=pruned["audit_prune_run_id"],
                expected_receipt_sha256=receipt_sha256,
                segment=target.name,
                expected_segment_sha256=segment_sha256,
                decision="preserve-recreated",
            )

            self.assertFalse(resumed["already_resolved"])
            self.assertEqual(marker_path.read_bytes(), marker_before)
            self.assertTrue(receipt_path.is_file())

    def test_audit_prune_resolve_refuses_fence_and_receipt_ambiguity(self):
        cases = (
            "receipt_hash",
            "segment_hash",
            "run_id",
            "segment",
            "invalid_receipt",
            "multiple_receipts",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (
                    target,
                    receipt_path,
                    receipt_sha256,
                    segment_sha256,
                    pruned,
                ) = self._create_applied_recreated_prune(root)
                run_id = pruned["audit_prune_run_id"]
                segment = target.name
                expected_receipt = receipt_sha256
                expected_segment = segment_sha256
                if case == "receipt_hash":
                    expected_receipt = "0" * 64
                elif case == "segment_hash":
                    expected_segment = "0" * 64
                elif case == "run_id":
                    run_id = "0" * 64
                elif case == "segment":
                    segment = "events.2.jsonl"
                elif case == "invalid_receipt":
                    receipt_path.write_text("{invalid}\n", encoding="utf-8")
                    expected_receipt = hashlib.sha256(
                        receipt_path.read_bytes()
                    ).hexdigest()
                elif case == "multiple_receipts":
                    duplicate = receipt_path.with_name(f"{'0' * 64}.json")
                    duplicate.write_bytes(receipt_path.read_bytes())

                before_target = target.read_bytes()
                before_receipts = {
                    path.name: path.read_bytes()
                    for path in receipt_path.parent.iterdir()
                    if path.is_file()
                }
                completed, _ = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-prune-resolve",
                    "--run-id",
                    run_id,
                    "--expected-receipt-sha256",
                    expected_receipt,
                    "--segment",
                    segment,
                    "--expected-segment-sha256",
                    expected_segment,
                    "--decision",
                    "preserve-recreated",
                    "--root",
                    str(root),
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("audit prune resolve refused", completed.stderr)
                self.assertEqual(target.read_bytes(), before_target)
                self.assertEqual(
                    {
                        path.name: path.read_bytes()
                        for path in receipt_path.parent.iterdir()
                        if path.is_file()
                    },
                    before_receipts,
                )
                self.assertFalse((target.parent / ".rotated-preserve").exists())

    def test_audit_prune_resolve_requires_the_oa_writer_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (
                target,
                receipt_path,
                receipt_sha256,
                segment_sha256,
                pruned,
            ) = self._create_applied_recreated_prune(root)
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "audit-prune-resolve",
                "--run-id",
                pruned["audit_prune_run_id"],
                "--expected-receipt-sha256",
                receipt_sha256,
                "--segment",
                target.name,
                "--expected-segment-sha256",
                segment_sha256,
                "--decision",
                "preserve-recreated",
                "--root",
                str(root),
                env={"PAO_OA_ID": ""},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("PAO_OA_ID is required", completed.stderr)
            self.assertTrue(receipt_path.is_file())
            self.assertFalse((target.parent / ".rotated-preserve").exists())

    def test_audit_prune_removes_only_old_fully_bound_committed_repair_pair(self):
        for segment in ("events.1.jsonl", "events.jsonl", "degraded.jsonl"):
            with self.subTest(segment=segment), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "var" / "audit" / segment
                target.parent.mkdir(parents=True)
                original = b'{"event":"valid"}\n{bad}\n'
                target.write_bytes(original)
                digest = hashlib.sha256(original).hexdigest()
                _, repaired = self.run_module(
                    "pao_runtime.oa_cli",
                    "audit-repair",
                    "--segment",
                    segment,
                    "--expected-sha256",
                    digest,
                    "--drop-line",
                    "2",
                    "--root",
                    str(root),
                    expected=0,
                )
                receipt_path = target.parent / repaired["receipt"]
                backup = target.parent / repaired["backup"]
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["committed_at"] = (
                    datetime.now(timezone.utc) - timedelta(days=2)
                ).isoformat().replace("+00:00", "Z")
                receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

                _, pruned = self.run_module(
                    "pao_runtime.oa_cli",
                    "prune",
                    "--older-than-days",
                    "1",
                    "--root",
                    str(root),
                    expected=0,
                )
                self.assertEqual(pruned["repair_receipts_removed"], 1)
                self.assertEqual(pruned["repair_backups_removed"], 1)
                self.assertEqual(pruned["total"], 2)
                self.assertFalse(receipt_path.exists())
                self.assertFalse(backup.exists())
                if segment == "degraded.jsonl":
                    self.assertFalse(target.exists())
                else:
                    self.assertTrue(target.is_file())

    def test_audit_repair_retention_hard_crashes_converge_at_every_delete_boundary(self):
        cases = {
            "after_receipt_delete": 91,
            "after_backup_stage": 92,
            "after_staged_delete": 93,
        }
        fault_code = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from datetime import datetime,timezone\n"
            "from pao_runtime import audit\n"
            "mode=sys.argv[2]\n"
            "real_unlink=Path.unlink\n"
            "real_replace=audit._replace_retry\n"
            "def crash_unlink(self,*args,**kwargs):\n"
            " result=real_unlink(self,*args,**kwargs)\n"
            " if mode=='after_receipt_delete' and self.parent.name=='.repairs':\n"
            "  os._exit(91)\n"
            " if mode=='after_staged_delete' and self.parent.name=='.repair-prune' and self.name.endswith('.repair-original'):\n"
            "  os._exit(93)\n"
            " return result\n"
            "def crash_replace(source,destination,*args,**kwargs):\n"
            " result=real_replace(source,destination,*args,**kwargs)\n"
            " destination=Path(destination)\n"
            " if mode=='after_backup_stage' and destination.parent.name=='.repair-prune' and destination.name.endswith('.repair-original'):\n"
            "  os._exit(92)\n"
            " return result\n"
            "Path.unlink=crash_unlink\n"
            "audit._replace_retry=crash_replace\n"
            "audit.prune_committed_repairs(Path(sys.argv[1]),datetime.now(timezone.utc))\n"
        )
        for mode, exit_code in cases.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, receipt_path, backup = self._create_old_committed_repair(root)
                audit_dir = receipt_path.parents[1]
                crashed = subprocess.run(
                    [sys.executable, "-c", fault_code, str(root), mode],
                    cwd=REPO,
                    env={**os.environ, "PYTHONPATH": str(RUNTIME_HOME)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    crashed.returncode, exit_code, crashed.stderr + crashed.stdout
                )
                tombstone = next((audit_dir / ".repair-prune").glob("*.json"))
                transaction = audit._load_repair_retention(tombstone)
                staged = audit_dir / transaction["staged_backup"]
                self.assertFalse(receipt_path.exists())
                if mode == "after_receipt_delete":
                    self.assertTrue(backup.is_file())
                    self.assertFalse(staged.exists())
                    self.assertEqual(transaction["phase"], "authorized")
                elif mode == "after_backup_stage":
                    self.assertFalse(backup.exists())
                    self.assertTrue(staged.is_file())
                    self.assertEqual(transaction["phase"], "authorized")
                else:
                    self.assertFalse(backup.exists())
                    self.assertFalse(staged.exists())
                    self.assertEqual(transaction["phase"], "backup_staged")

                stale_time = time.time() - 31
                for lock in (audit_dir / ".audit.lock", audit_dir / ".degraded.lock"):
                    self.assertTrue(lock.is_file())
                    os.utime(lock, (stale_time, stale_time))
                counts = audit.prune_committed_repairs(
                    root, datetime.now(timezone.utc) - timedelta(days=100)
                )
                self.assertEqual(counts["repair_receipts_removed"], 0)
                self.assertEqual(
                    counts["repair_backups_removed"],
                    0 if mode == "after_staged_delete" else 1,
                )
                self.assertFalse(receipt_path.exists())
                self.assertFalse(backup.exists())
                self.assertFalse(staged.exists())
                self.assertFalse(tombstone.exists())

    def test_audit_health_classifies_every_retention_crash_topology_as_resumable(self):
        cases = (
            "authorized_receipt_and_backup",
            "authorized_backup_only",
            "authorized_staged_backup",
            "backup_staged_with_file",
            "backup_staged_after_delete",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, receipt_path, backup = self._create_old_committed_repair(root)
                audit_dir = receipt_path.parents[1]
                real_unlink = Path.unlink

                def refuse_receipt_unlink(path, *args, **kwargs):
                    if path == receipt_path:
                        raise OSError("stop after retention authorization")
                    return real_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", refuse_receipt_unlink):
                    audit.prune_committed_repairs(root, datetime.now(timezone.utc))
                tombstone = next((audit_dir / ".repair-prune").glob("*.json"))
                transaction = json.loads(tombstone.read_text(encoding="utf-8"))
                staged = audit_dir / transaction["staged_backup"]

                if case != "authorized_receipt_and_backup":
                    receipt_path.unlink()
                if case in {
                    "authorized_staged_backup",
                    "backup_staged_with_file",
                    "backup_staged_after_delete",
                }:
                    os.replace(backup, staged)
                if case in {"backup_staged_with_file", "backup_staged_after_delete"}:
                    transaction["phase"] = "backup_staged"
                    transaction["staged_at"] = utc_now()
                    tombstone.write_text(
                        json.dumps(transaction) + "\n", encoding="utf-8"
                    )
                if case == "backup_staged_after_delete":
                    staged.unlink()

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
                    expected=0,
                )
                after = {
                    path.relative_to(root).as_posix(): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertEqual(report["status"], "attention")
                self.assertEqual(report["resumable_retention_count"], 1)
                self.assertEqual(report["blocked_retention_count"], 0)
                self.assertEqual(
                    report["retention_tombstones"][0]["status"], "resumable"
                )
                self.assertEqual(
                    report["retention_tombstones"][0]["reason_codes"], []
                )
                self.assertTrue(
                    any("Run prune" in item for item in report["guidance"])
                )

    def test_audit_repair_retention_tombstone_drift_fails_closed(self):
        cases = {
            "invalid_tombstone": "invalid_tombstone",
            "receipt_drift": "receipt_invalid",
            "audit_key_missing": "audit_key_missing",
            "target_drift": "target_not_repaired",
            "backup_drift": "backup_invalid",
            "conflicting_stage": "inconsistent_file_state",
            "missing_backup": "inconsistent_file_state",
        }
        for case, expected_reason in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, receipt_path, backup = self._create_old_committed_repair(root)
                audit_dir = receipt_path.parents[1]
                real_unlink = Path.unlink

                def refuse_receipt_unlink(path, *args, **kwargs):
                    if path == receipt_path:
                        raise OSError("stop after retention authorization")
                    return real_unlink(path, *args, **kwargs)

                with mock.patch.object(Path, "unlink", refuse_receipt_unlink):
                    counts = audit.prune_committed_repairs(
                        root, datetime.now(timezone.utc)
                    )
                self.assertEqual(
                    counts,
                    {"repair_receipts_removed": 0, "repair_backups_removed": 0},
                )
                tombstone = next((audit_dir / ".repair-prune").glob("*.json"))
                transaction = json.loads(tombstone.read_text(encoding="utf-8"))
                staged = audit_dir / transaction["staged_backup"]

                if case == "invalid_tombstone":
                    tombstone.write_text("{invalid}\n", encoding="utf-8")
                elif case == "receipt_drift":
                    receipt_path.write_text(
                        receipt_path.read_text(encoding="utf-8") + " ",
                        encoding="utf-8",
                    )
                elif case == "audit_key_missing":
                    (audit_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
                elif case == "target_drift":
                    target.write_text('{"event":"drift"}\n', encoding="utf-8")
                elif case == "backup_drift":
                    backup.write_bytes(b"drift")
                elif case == "conflicting_stage":
                    staged.write_bytes(backup.read_bytes())
                elif case == "missing_backup":
                    backup.unlink()

                counts = audit.prune_committed_repairs(
                    root, datetime.now(timezone.utc)
                )
                self.assertEqual(
                    counts,
                    {"repair_receipts_removed": 0, "repair_backups_removed": 0},
                )
                self.assertTrue(tombstone.is_file())
                self.assertTrue(receipt_path.is_file())
                if case != "missing_backup":
                    self.assertTrue(backup.is_file())
                if case == "conflicting_stage":
                    self.assertTrue(staged.is_file())
                health = audit.health(root)
                self.assertEqual(health["status"], "attention")
                self.assertEqual(health["resumable_retention_count"], 0)
                self.assertEqual(health["blocked_retention_count"], 1)
                self.assertEqual(
                    health["retention_tombstones"][0]["status"], "blocked"
                )
                self.assertIn(
                    expected_reason,
                    health["retention_tombstones"][0]["reason_codes"],
                )
                self.assertTrue(
                    any(
                        "Do not delete blocked" in item
                        for item in health["guidance"]
                    )
                )

    def test_audit_prune_preserves_noncommitted_invalid_recent_and_drifted_repairs(self):
        cases = (
            "prepared",
            "replaced",
            "invalid_receipt",
            "recent_committed",
            "target_drift",
            "target_missing",
            "backup_drift",
            "audit_key_missing",
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        old_committed_at = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat().replace("+00:00", "Z")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "var" / "audit" / "events.1.jsonl"
                target.parent.mkdir(parents=True)
                original = b'{"event":"valid"}\n{bad}\n'
                target.write_bytes(original)
                digest = hashlib.sha256(original).hexdigest()

                if case == "prepared":
                    with mock.patch.object(audit, "_replace_retry", side_effect=OSError("stop")):
                        with self.assertRaisesRegex(OSError, "stop"):
                            audit.repair(root, "events.1.jsonl", digest, [2])
                    receipt_path = next((target.parent / ".repairs").glob("*.json"))
                    receipt = audit._load_repair_receipt(receipt_path)
                elif case in {"replaced", "audit_key_missing"}:
                    report = audit.repair(root, "events.1.jsonl", digest, [2])
                    receipt_path = target.parent / report["receipt"]
                    receipt = audit._load_repair_receipt(receipt_path)
                    if case == "audit_key_missing":
                        receipt = {
                            **receipt,
                            "phase": "committed",
                            "audit_event_committed": True,
                            "committed_at": old_committed_at,
                        }
                        receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
                else:
                    _, report = self.run_module(
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
                    receipt_path = target.parent / report["receipt"]
                    receipt = audit._load_repair_receipt(receipt_path)
                    if case != "recent_committed":
                        receipt["committed_at"] = old_committed_at
                        receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

                backup = target.parent / receipt["backup"]
                if case == "invalid_receipt":
                    receipt_path.write_text("{invalid}\n", encoding="utf-8")
                elif case == "target_drift":
                    target.write_bytes(b'{"event":"drifted"}\n')
                elif case == "target_missing":
                    target.unlink()
                elif case == "backup_drift":
                    backup.write_bytes(b"drifted backup")

                counts = audit.prune_committed_repairs(root, cutoff)
                self.assertEqual(
                    counts,
                    {"repair_receipts_removed": 0, "repair_backups_removed": 0},
                )
                self.assertTrue(receipt_path.is_file())
                self.assertTrue(backup.is_file())

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
            self.assertEqual(report["retention_tombstones"], [])
            self.assertEqual(report["resumable_retention_count"], 0)
            self.assertEqual(report["blocked_retention_count"], 0)
            self.assertEqual(report["rotated_prune_receipts"], [])
            self.assertEqual(report["resumable_rotated_prune_count"], 0)
            self.assertEqual(report["blocked_rotated_prune_count"], 0)
            self.assertEqual(report["rotated_preservations"], [])
            self.assertEqual(report["protected_rotated_preservation_count"], 0)
            self.assertEqual(report["blocked_rotated_preservation_count"], 0)
            self.assertEqual(report["preservation_releases"], [])
            self.assertEqual(report["completed_preservation_release_count"], 0)
            self.assertEqual(report["resumable_preservation_release_count"], 0)
            self.assertEqual(report["blocked_preservation_release_count"], 0)
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
            self.assertEqual(repaired["receipt_phase"], "committed")
            self.assertFalse(repaired["resumed"])
            self.assertFalse(repaired["already_repaired"])
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
            self.assertEqual(health["pending_repair_count"], 0)
            self.assertEqual(health["repair_receipts"][0]["phase"], "committed")

            before_retry = {
                path.relative_to(audit_dir).as_posix(): path.read_bytes()
                for path in audit_dir.rglob("*")
                if path.is_file()
            }
            _, retried = self.run_module(
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
            self.assertTrue(retried["resumed"])
            self.assertTrue(retried["already_repaired"])
            self.assertEqual(retried["receipt_phase"], "committed")
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
            receipts = list((target.parent / ".repairs").glob("*.json"))
            self.assertEqual(len(receipts), 1)
            self.assertEqual(audit._load_repair_receipt(receipts[0])["phase"], "prepared")
            interrupted_health = audit.health(root)
            self.assertEqual(interrupted_health["pending_repair_count"], 1)
            self.assertEqual(interrupted_health["repair_receipts"][0]["phase"], "prepared")

            resumed = audit.repair(root, "events.1.jsonl", digest, [2])
            self.assertTrue(resumed["resumed"])
            self.assertFalse(resumed["already_repaired"])
            self.assertEqual(resumed["receipt_phase"], "replaced")
            self.assertEqual(target.read_bytes(), b'{"event":"valid"}\n')

    def test_audit_repair_recovers_after_replace_before_receipt_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "var" / "audit" / "events.1.jsonl"
            target.parent.mkdir(parents=True)
            original = b'{"event":"valid"}\n{bad}\n'
            repaired = b'{"event":"valid"}\n'
            target.write_bytes(original)
            digest = hashlib.sha256(original).hexdigest()
            real_atomic_write = audit.atomic_write_json

            def fail_replaced_receipt(path, payload):
                if payload.get("schema_version") == audit.AUDIT_REPAIR_RECEIPT_SCHEMA and payload.get("phase") == "replaced":
                    raise OSError("crash after target replace")
                return real_atomic_write(path, payload)

            with mock.patch.object(audit, "atomic_write_json", side_effect=fail_replaced_receipt):
                with self.assertRaisesRegex(OSError, "crash after target replace"):
                    audit.repair(root, "events.1.jsonl", digest, [2])
            self.assertEqual(target.read_bytes(), repaired)
            receipt_path = next((target.parent / ".repairs").glob("*.json"))
            self.assertEqual(audit._load_repair_receipt(receipt_path)["phase"], "prepared")

            resumed = audit.repair(root, "events.1.jsonl", digest, [2])
            self.assertTrue(resumed["resumed"])
            self.assertTrue(resumed["already_repaired"])
            self.assertEqual(resumed["receipt_phase"], "replaced")
            self.assertEqual(target.read_bytes(), repaired)

    def test_audit_repair_retry_closes_crash_before_and_after_audit_commit(self):
        for audit_before_retry in (False, True):
            with self.subTest(audit_before_retry=audit_before_retry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "var" / "audit" / "events.1.jsonl"
                target.parent.mkdir(parents=True)
                original = b'{"event":"valid"}\n{bad}\n'
                target.write_bytes(original)
                digest = hashlib.sha256(original).hexdigest()
                interrupted = audit.repair(root, "events.1.jsonl", digest, [2])
                self.assertEqual(interrupted["receipt_phase"], "replaced")
                if audit_before_retry:
                    self.assertTrue(
                        audit.record_once(
                            root,
                            "oa",
                            {
                                "event": "audit_repair_committed",
                                "segment": interrupted["segment"],
                                "original_sha256": interrupted["original_sha256"],
                                "repaired_sha256": interrupted["repaired_sha256"],
                                "dropped_lines": interrupted["dropped_lines"],
                                "backup": interrupted["backup"],
                            },
                            f"audit-repair:{interrupted['segment']}:{interrupted['original_sha256']}",
                        )
                    )

                _, converged = self.run_module(
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
                self.assertTrue(converged["resumed"])
                self.assertTrue(converged["already_repaired"])
                self.assertEqual(converged["receipt_phase"], "committed")
                repair_events = []
                for path in sorted(target.parent.glob("events*.jsonl")):
                    repair_events.extend(
                        json.loads(line)
                        for line in path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    )
                self.assertEqual(
                    sum(event.get("event") == "audit_repair_committed" for event in repair_events),
                    1,
                )

    def test_audit_repair_receipt_and_target_drift_fail_closed(self):
        for drift in ("receipt", "target"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / "var" / "audit" / "events.1.jsonl"
                target.parent.mkdir(parents=True)
                original = b'{"event":"valid"}\n{bad}\n'
                target.write_bytes(original)
                digest = hashlib.sha256(original).hexdigest()
                with mock.patch.object(audit, "_replace_retry", side_effect=OSError("stop")):
                    with self.assertRaisesRegex(OSError, "stop"):
                        audit.repair(root, "events.1.jsonl", digest, [2])
                receipt_path = next((target.parent / ".repairs").glob("*.json"))
                if drift == "receipt":
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    receipt["repaired_sha256"] = "0" * 64
                    receipt_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
                    message = "candidate fingerprint does not match receipt"
                else:
                    target.write_bytes(b'{"event":"unexpected"}\n')
                    message = "target drift"
                before = target.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    audit.repair(root, "events.1.jsonl", digest, [2])
                self.assertEqual(target.read_bytes(), before)

    def test_audit_repair_hard_process_crash_converges_after_stale_lock_reap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "var" / "audit" / "events.1.jsonl"
            target.parent.mkdir(parents=True)
            original = b'{"event":"valid"}\n{bad}\n'
            repaired = b'{"event":"valid"}\n'
            target.write_bytes(original)
            digest = hashlib.sha256(original).hexdigest()
            code = (
                "import os, sys\n"
                "from pathlib import Path\n"
                "from pao_runtime import audit\n"
                "real = audit._replace_retry\n"
                "def crash(source, destination):\n"
                "    real(source, destination)\n"
                "    os._exit(91)\n"
                "audit._replace_retry = crash\n"
                "audit.repair(Path(sys.argv[1]), 'events.1.jsonl', sys.argv[2], [2])\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", code, str(root), digest],
                cwd=REPO,
                env={**os.environ, "PYTHONPATH": str(RUNTIME_HOME)},
                check=False,
            )
            self.assertEqual(completed.returncode, 91)
            self.assertEqual(target.read_bytes(), repaired)
            interrupted = audit.health(root)
            self.assertEqual(interrupted["pending_repair_count"], 1)
            self.assertEqual(interrupted["repair_receipts"][0]["phase"], "prepared")

            stale_time = time.time() - 31
            for lock in (target.parent / ".audit.lock", target.parent / ".degraded.lock"):
                self.assertTrue(lock.is_file())
                os.utime(lock, (stale_time, stale_time))
            _, converged = self.run_module(
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
            self.assertTrue(converged["resumed"])
            self.assertTrue(converged["already_repaired"])
            self.assertEqual(converged["receipt_phase"], "committed")
            self.assertEqual(target.read_bytes(), repaired)

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
