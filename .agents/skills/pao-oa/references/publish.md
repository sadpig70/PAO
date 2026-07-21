# OA Reference — Task Publication

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Task draft

Write a task draft file first:

```json
{
  "goal": "Requested objective",
  "instructions": "Concrete instructions",
  "completion_criteria": ["Verification criteria"],
  "cwd": "/absolute/workspace/project",
  "timeout_s": 90,
  "priority": 5,
  "permissions": {"read": [], "write": [], "network": false}
}
```

- **Use absolute paths.** Both `--task-file` and the draft's `cwd` (and any
  `permissions.read`/`write` entries) are resolved against the **OA process cwd**,
  not the bus root — a relative value silently binds to wherever the OA happens to
  run. Always write an absolute `cwd` and pass an absolute `--task-file`.
- `cwd` must exist; `send` rejects tasks whose `cwd` does not exist.
- **Authority bounds** (enforced, not advisory): `send` rejects a `cwd` or a `permissions.read`/`write` entry at-or-under the bus control surfaces (`mailbox/`, `var/`, `control/`) or the runtime bundle. The bus root itself and other subdirectories stay legal. The claim-side watcher re-checks the cwd deny-set against hand-planted tasks.
- Omitted `permissions` defaults to `{"read": [cwd], "write": [cwd], "network": false}`. Optional `permissions.max_artifact_bytes` (positive integer) caps each artifact at submission.
- State match strictness in `completion_criteria` (e.g. whether a trailing newline is acceptable) — LWARs default to byte-exact when a criterion says "exactly".
- A draft may declare `depends_on: ["task-..."]`. Publication is blocked until every dependency is `completed` with a `succeeded` result **and** has a recorded `validation.semantic_verdict=accepted` whose criteria all passed.

## Commands

```bash
python "<PAO_SKILL>/scripts/oa.py" send --lwar-id LWAR1 --task-file TASK_DRAFT.json
python "<PAO_SKILL>/scripts/oa.py" send --auto --require-capability coding --task-file TASK_DRAFT.json
```

## Rules

- The OA tool binds `instance_id`, `generation`, and `registry_version` from the registry into the task. OA must not edit mailbox files directly.
- `send` (like every mutating OA command) refreshes the single-writer lease; see SKILL.md §1 — set `PAO_OA_ID` once per session.
- `--auto` routes by capability and load: only `on` LWARs with a fresh, valid heartbeat and every `--require-capability` are eligible; ties break toward the lowest backlog, then the lowest LWAR number. No eligible LWAR is an explicit error — never fall back to an arbitrary or stale LWAR.
- Every publication uses a durable outbox sequence: ledger `publishing` → atomic mailbox publish → ledger `published`. `recover` repairs an interruption between these steps from the stored TaskContract.
- `workflow_id` and `task_id` are schema-validated safe identifiers; values that could escape `var/tasks/` are rejected.
- The claim lease is aligned with the task budget: `effective_lease_s = max(lease_seconds, timeout_s + 30)`.

## Plan — turning a goal into TaskContracts

Planning is OA judgment; the mechanics to get right:

- **One workflow, many tasks**: set the same `workflow_id` on related drafts so
  `workflow-status --workflow-id …` tracks them as a unit. Omitting it mints a
  fresh workflow per task.
- **Ordering**: use `depends_on: ["task-…"]` to gate a successor until its
  dependencies are `completed`, `succeeded`, and semantically `accepted`. Keep the graph acyclic;
  a task must not depend on itself or on an unpublished task.
- **Completion criteria quality**: write criteria that are **mechanically
  checkable** (a file exists with exact content, a test command exits 0, an
  artifact hash) rather than subjective — the LWAR submits evidence against them
  and OA `validate` re-checks. State match strictness explicitly (e.g. "exactly,
  no trailing newline").
- **Bounds**: give each task the tightest `permissions` (cwd-scoped read/write,
  `network: false` unless required) and a realistic `timeout_s`. `max_retries`
  (default 3) caps recovery attempts before dead-letter.
- **Satisfiability**: every criterion must be achievable within the task's own
  authority bounds — a criterion the task has no permission to satisfy forces an
  honest `blocked`.
