from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import (
    atomic_write_json,
    path_within,
    safe_load_json,
    utc_now,
    validate_task_id,
    validate_workflow_id,
)
from .contracts import validate_contract


class TaskLedger:
    """OA-side durable record of every published task's lifecycle.

    Entries live under var/tasks/{workflow_id}/{task_id}.json and move through
    published -> requeued* -> completed | dead. The ledger is OA state; LWAR
    contracts never write it.
    """

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.base = self.root / "var" / "tasks"

    def path(self, workflow_id: str, task_id: str) -> Path:
        workflow_id = validate_workflow_id(workflow_id)
        task_id = validate_task_id(task_id)
        target = (self.base / workflow_id / f"{task_id}.json").resolve()
        if not path_within(target, self.base):
            raise ValueError("task ledger path escapes var/tasks")
        return target

    def _write(self, entry: dict[str, Any]) -> None:
        entry["updated_at"] = utc_now()
        validate_contract(entry, "task-ledger.schema.json")
        atomic_write_json(self.path(entry["workflow_id"], entry["task_id"]), entry)

    def record_publishing(self, task: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "schema_version": "pao.task-ledger.v1",
            "task_id": task["task_id"],
            "workflow_id": task["workflow_id"],
            "lwar_id": task["lwar_id"],
            "instance_id": task["instance_id"],
            "generation": task["generation"],
            "goal": task["goal"],
            "completion_criteria": task.get("completion_criteria", []),
            "depends_on": task.get("depends_on", []),
            "status": "publishing",
            "attempt": int(task.get("attempt", 1)),
            "max_retries": int(task.get("max_retries", 3)),
            "published_at": task.get("created_at", utc_now()),
            "result": None,
            "result_file": None,
            "task_contract": task,
            "history": [{"status": "publishing", "at": utc_now(), "detail": None}],
        }
        self._write(entry)
        return entry

    def record_published(self, task: dict[str, Any]) -> dict[str, Any]:
        """Compatibility helper for callers that already published the task."""
        entry = self.record_publishing(task)
        return self.transition(
            task["task_id"], "published", workflow_id=task["workflow_id"], detail="published"
        ) or entry

    def get(self, task_id: str, workflow_id: str | None = None) -> dict[str, Any] | None:
        if workflow_id:
            path = self.path(workflow_id, task_id)
            return safe_load_json(path) if path.is_file() else None
        for path in sorted(self.base.glob(f"*/{task_id}.json")):
            entry = safe_load_json(path)
            if entry is not None:
                return entry
        return None

    def transition(
        self,
        task_id: str,
        status: str,
        workflow_id: str | None = None,
        detail: str | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        entry = self.get(task_id, workflow_id)
        if entry is None:
            return None
        entry["status"] = status
        entry.update(fields)
        entry.setdefault("history", []).append({"status": status, "at": utc_now(), "detail": detail})
        self._write(entry)
        return entry

    def record_completed_result(self, result: dict[str, Any], result_file: str) -> dict[str, Any] | None:
        """Mark a task completed from a collected result; creates a lenient
        entry when the task predates the ledger."""
        workflow_id = result.get("workflow_id")
        entry = self.transition(
            result["task_id"],
            "completed",
            workflow_id=workflow_id,
            detail=f"result:{result.get('status')}",
            result=result,
            result_file=result_file,
        )
        if entry is not None or not workflow_id:
            return entry
        entry = {
            "schema_version": "pao.task-ledger.v1",
            "task_id": result["task_id"],
            "workflow_id": workflow_id,
            "lwar_id": result.get("lwar_id"),
            "instance_id": result.get("instance_id"),
            "generation": result.get("generation"),
            "goal": None,
            "completion_criteria": [],
            "depends_on": [],
            "status": "completed",
            "attempt": None,
            "max_retries": None,
            "published_at": None,
            "result": result,
            "result_file": result_file,
            "history": [{"status": "completed", "at": utc_now(), "detail": "ledger_backfill"}],
        }
        self._write(entry)
        return entry

    def record_validation(
        self, task_id: str, workflow_id: str | None, decision: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Attach an OA ValidationDecision; the result payload stays immutable."""
        entry = self.get(task_id, workflow_id)
        if entry is None:
            return None
        entry["validation"] = decision
        self._write(entry)
        return entry

    def update_result_file(
        self, task_id: str, workflow_id: str, result_file: str
    ) -> dict[str, Any] | None:
        entry = self.get(task_id, workflow_id)
        if entry is None:
            return None
        entry["result_file"] = result_file
        self._write(entry)
        return entry

    def all_entries(self) -> list[dict[str, Any]]:
        entries = []
        if not self.base.is_dir():
            return entries
        for path in sorted(self.base.glob("*/*.json")):
            entry = safe_load_json(path)
            if entry is not None:
                entries.append(entry)
        return entries

    def referenced_artifacts(self) -> set[str]:
        referenced = set()
        for entry in self.all_entries():
            for artifact in (entry.get("result") or {}).get("artifacts", []):
                if isinstance(artifact, dict) and isinstance(artifact.get("snapshot"), str):
                    referenced.add(artifact["snapshot"])
        return referenced

    def workflow_entries(self, workflow_id: str) -> list[dict[str, Any]]:
        workflow_id = validate_workflow_id(workflow_id)
        directory = self.base / workflow_id
        if not directory.is_dir():
            return []
        entries = []
        for path in sorted(directory.glob("*.json")):
            entry = safe_load_json(path)
            if entry is not None:
                entries.append(entry)
        return entries
