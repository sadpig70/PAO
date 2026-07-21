import importlib.util
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from pao_helpers import REPO, PaoTestCase
from pao_runtime import audit, oa_cli
from pao_runtime.common import load_json, local_filesystem_status, utc_now
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
