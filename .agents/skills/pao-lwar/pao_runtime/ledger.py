from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import atomic_write_json, load_json, utc_now


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
        return self.base / workflow_id / f"{task_id}.json"

    def _write(self, entry: dict[str, Any]) -> None:
        entry["updated_at"] = utc_now()
        atomic_write_json(self.path(entry["workflow_id"], entry["task_id"]), entry)

    def record_published(self, task: dict[str, Any]) -> dict[str, Any]:
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
            "status": "published",
            "attempt": int(task.get("attempt", 1)),
            "max_retries": int(task.get("max_retries", 3)),
            "published_at": task.get("created_at", utc_now()),
            "result": None,
            "result_file": None,
            "history": [{"status": "published", "at": utc_now(), "detail": None}],
        }
        self._write(entry)
        return entry

    def get(self, task_id: str, workflow_id: str | None = None) -> dict[str, Any] | None:
        if workflow_id:
            path = self.path(workflow_id, task_id)
            return load_json(path) if path.is_file() else None
        for path in sorted(self.base.glob(f"*/{task_id}.json")):
            return load_json(path)
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

    def workflow_entries(self, workflow_id: str) -> list[dict[str, Any]]:
        directory = self.base / workflow_id
        if not directory.is_dir():
            return []
        return [load_json(path) for path in sorted(directory.glob("*.json"))]
