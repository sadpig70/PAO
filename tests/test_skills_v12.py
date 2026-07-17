import json
import os
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase


def ledger_entry(root, task_id):
    for path in sorted((root / "var" / "tasks").glob(f"*/{task_id}.json")):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def write_draft(root, name, payload):
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class SendAuthorityBoundsTests(PaoTestCase):
    def test_cwd_inside_bus_control_surface_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            bad_cwd = root / "mailbox" / "LWAR1" / "work"
            draft = write_draft(root, "bad.json", {"goal": "escape", "cwd": str(bad_cwd)})
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "send", "--lwar-id", "LWAR1",
                "--task-file", str(draft), "--root", str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("authority bounds", completed.stderr)

    def test_write_permission_under_var_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            draft = write_draft(root, "bad_perm.json", {
                "goal": "sneaky",
                "cwd": str(root),
                "permissions": {"read": [], "write": [str(root / "var" / "tasks")], "network": False},
            })
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "send", "--lwar-id", "LWAR1",
                "--task-file", str(draft), "--root", str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("permissions.write", completed.stderr)

    def test_bus_root_cwd_and_default_permissions_stay_legal(self):
        # The deny-set covers control surfaces, never their ancestor (the root).
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "root cwd ok"})
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            task = json.loads(incoming[0].read_text(encoding="utf-8"))
            resolved_root = str(root.resolve())
            self.assertEqual(task["permissions"]["read"], [resolved_root])
            self.assertEqual(task["permissions"]["write"], [resolved_root])
            self.assertFalse(task["permissions"]["network"])


class ClaimGuardTests(PaoTestCase):
    def test_planted_task_with_denied_cwd_is_rejected_at_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            planted = {
                "schema_version": "pao.task.v1",
                "task_id": "task-planted-1",
                "workflow_id": "workflow-planted",
                "lwar_id": "LWAR1",
                "instance_id": adopted["instance_id"],
                "generation": adopted["generation"],
                "goal": "hand-planted escape",
                "cwd": str(root / "var"),
                "created_at": "2026-01-01T00:00:00Z",
            }
            incoming = root / "mailbox" / "LWAR1" / "incoming" / "005_task-planted-1.json"
            incoming.write_text(json.dumps(planted), encoding="utf-8")
            _, event = self.watch_once(root, adopted, expected=10)
            self.assertEqual(event["event"], "idle_timeout")
            errors = list((root / "mailbox" / "LWAR1" / "failed").glob("*.error.json"))
            self.assertEqual(len(errors), 1)
            reason = json.loads(errors[0].read_text(encoding="utf-8"))["reason"]
            self.assertIn("authority_violation:inside_bus_var", reason)

    def test_recover_reconciles_rejected_task_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "will be rejected"})
            task_id = sent["task_id"]
            # Simulate a claim-side rejection of the published task.
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))[0]
            failed = root / "mailbox" / "LWAR1" / "failed" / incoming.name
            failed.parent.mkdir(parents=True, exist_ok=True)
            incoming.replace(failed)
            failed.with_suffix(".error.json").write_text(
                json.dumps({"reason": "authority_violation:test", "failed_at": "2026-01-01T00:00:00Z"}),
                encoding="utf-8",
            )
            self.run_module("pao_runtime.oa_cli", "recover", "--root", str(root), expected=0)
            entry = ledger_entry(root, task_id)
            self.assertEqual(entry["status"], "failed")
            self.assertIn("authority_violation:test", entry["history"][-1]["detail"])


class ArtifactProvenanceTests(PaoTestCase):
    def _run_task_with_artifact(self, root, adopted, artifact_body=b"artifact-body", declare=None,
                                draft_extra=None, expected_complete=0):
        ws = root / "ws"
        ws.mkdir(exist_ok=True)
        draft = {"goal": "produce artifact", "cwd": str(ws)}
        draft.update(draft_extra or {})
        _, sent = self.send_task(root, "LWAR1", draft)
        _, event = self.watch_once(root, adopted, expected=0)
        task_id = event["task_id"]
        artifact = ws / "out.bin"
        artifact.write_bytes(artifact_body)
        result = {
            "status": "succeeded",
            "summary": "made artifact",
            "evidence": {"ok": True},
            "artifacts": declare if declare is not None else ["out.bin"],
            "exit_code": 0,
        }
        completed, payload = self.complete_task(
            root, adopted, task_id, result=result, expected=expected_complete
        )
        return task_id, completed

    def test_complete_snapshots_artifacts_as_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            task_id, _ = self._run_task_with_artifact(root, adopted)
            _, collected = self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            self.assertEqual(collected["count"], 1)
            artifact = collected["results"][0]["result"]["artifacts"][0]
            self.assertRegex(artifact["sha256"], r"^[a-f0-9]{64}$")
            self.assertEqual(artifact["size_bytes"], len(b"artifact-body"))
            snapshot = root / artifact["snapshot"]
            self.assertTrue(snapshot.is_file())
            self.assertEqual(snapshot.read_bytes(), b"artifact-body")

    def test_complete_fails_on_missing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            _, completed = self._run_task_with_artifact(
                root, adopted, declare=["ghost.bin"], expected_complete=None
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("not a regular file", completed.stderr)

    def test_complete_rejects_artifact_outside_declared_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            outside = root / "elsewhere"
            outside.mkdir()
            (outside / "leak.bin").write_bytes(b"leak")
            _, completed = self._run_task_with_artifact(
                root, adopted, declare=[str(outside / "leak.bin")], expected_complete=None
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("outside allowed write roots", completed.stderr)

    def test_legacy_task_without_write_roots_gets_warning_passthrough(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            outside = root / "elsewhere"
            outside.mkdir()
            (outside / "legacy.bin").write_bytes(b"legacy")
            task_id, _ = self._run_task_with_artifact(
                root, adopted,
                declare=[str(outside / "legacy.bin")],
                draft_extra={"permissions": {"read": [], "write": [], "network": False}},
            )
            _, collected = self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            result = collected["results"][0]["result"]
            self.assertIsInstance(result["artifacts"][0], str)
            self.assertTrue(result["artifact_warnings"][0].startswith("outside_declared_roots:"))

    def test_max_artifact_bytes_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            ws_root = str((root / "ws").resolve())
            (root / "ws").mkdir(exist_ok=True)
            _, completed = self._run_task_with_artifact(
                root, adopted,
                artifact_body=b"0123456789",
                draft_extra={"permissions": {"read": [ws_root], "write": [ws_root],
                                             "network": False, "max_artifact_bytes": 4}},
                expected_complete=None,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("max_artifact_bytes", completed.stderr)

    def test_collect_quarantines_tampered_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self._run_task_with_artifact(root, adopted)
            snapshots = list((root / "var" / "artifacts").glob("*"))
            self.assertEqual(len(snapshots), 1)
            snapshots[0].write_bytes(b"TAMPERED-BYTES")
            _, collected = self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            self.assertEqual(collected["count"], 0)
            self.assertEqual(collected["quarantined"][0]["reason"], "artifact_tampered")


class ValidationDecisionTests(PaoTestCase):
    def test_record_persists_decision_and_plain_validate_stays_observer_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "validated job"})
            _, event = self.watch_once(root, adopted, expected=0)
            task_id = event["task_id"]
            self.complete_task(root, adopted, task_id)
            # The register helper's reconcile already holds the lease as
            # oa-default, so the writer in this scenario is the default holder.
            self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            # Plain validate from a DIFFERENT id while oa-default holds the lease.
            _, report = self.run_module(
                "pao_runtime.oa_cli", "validate", "--task-id", task_id, "--root", str(root),
                env={"PAO_OA_ID": "oa-observer"}, expected=0,
            )
            self.assertEqual(report["verdict"], "ready_for_oa_review")
            self.assertTrue(report["artifact_verification"]["verified"])
            self.assertFalse(report["recorded"])
            self.assertIsNone(ledger_entry(root, task_id).get("validation"))
            # --record from the writer id persists the decision.
            _, recorded = self.run_module(
                "pao_runtime.oa_cli", "validate", "--task-id", task_id, "--record",
                "--root", str(root), expected=0,
            )
            self.assertTrue(recorded["recorded"])
            decision = ledger_entry(root, task_id)["validation"]
            self.assertEqual(decision["schema_version"], "pao.validation-decision.v1")
            self.assertEqual(decision["verdict"], "ready_for_oa_review")
            self.assertEqual(decision["decided_by"], "oa-default")
            # --record from the observer id is rejected by the writer lease.
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "validate", "--task-id", task_id, "--record",
                "--root", str(root), env={"PAO_OA_ID": "oa-observer"},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("writer lease", completed.stderr)


if __name__ == "__main__":
    unittest.main()
