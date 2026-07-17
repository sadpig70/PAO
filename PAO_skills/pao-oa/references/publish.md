# OA Reference — Task Publication

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Task draft

Write a task draft file first:

```json
{
  "goal": "Requested objective",
  "instructions": "Concrete instructions",
  "completion_criteria": ["Verification criteria"],
  "cwd": "workspace/project",
  "timeout_s": 90,
  "priority": 5,
  "permissions": {"read": [], "write": [], "network": false}
}
```

- `cwd` must exist; `send` rejects tasks whose `cwd` does not exist.
- State match strictness in `completion_criteria` (e.g. whether a trailing newline is acceptable) — LWARs default to byte-exact when a criterion says "exactly".
- A draft may declare `depends_on: ["task-..."]`. Publication is blocked until every dependency is `completed` with a `succeeded` result in the task ledger.

## Commands

```bash
python "<PAO_SKILL>/scripts/oa.py" send --lwar-id LWAR1 --task-file TASK_DRAFT.json
python "<PAO_SKILL>/scripts/oa.py" send --auto --require-capability coding --task-file TASK_DRAFT.json
```

## Rules

- The OA tool binds `instance_id`, `generation`, and `registry_version` from the registry into the task. OA must not edit mailbox files directly.
- `send` (like every mutating OA command) refreshes the single-writer lease; see SKILL.md §1 — set `PAO_OA_ID` once per session.
- `--auto` routes by capability and load: only `on` LWARs holding every `--require-capability` are eligible; ties break toward the lowest backlog, then the lowest LWAR number. No eligible LWAR is an explicit error — never fall back to an arbitrary LWAR.
- Every publication is recorded in the task ledger at `var/tasks/{workflow_id}/{task_id}.json`.
- The claim lease is aligned with the task budget: `effective_lease_s = max(lease_seconds, timeout_s + 30)`.
