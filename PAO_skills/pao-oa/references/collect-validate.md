# OA Reference — Collection and Validation

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Commands

```bash
python "<PAO_SKILL>/scripts/oa.py" collect
python "<PAO_SKILL>/scripts/oa.py" collect --archive
python "<PAO_SKILL>/scripts/oa.py" validate --task-id TASK_ID
python "<PAO_SKILL>/scripts/oa.py" workflow-status --workflow-id WORKFLOW_ID
```

## Rules

- `collect` quarantines stale-generation, stale-attempt, and duplicate results into `quarantine/` and marks accepted tasks `completed` in the ledger. Quarantined results are never auto-approved.
- The attempt fence: results echo the `attempt` of the claim they came from; `recover` and `dead --requeue` bump the ledger's attempt, so a mismatched echo (`stale_attempt_result`) means the result belongs to a superseded claim. Results from pre-0.5 runtimes carry no echo and skip the fence.
- Every result is a **terminal submission, not a success claim** — only `status=succeeded` results are acceptance candidates; `failed`, `blocked`, `cancelled`, `interrupted`, `timed_out`, and `protocol_error` results are collected for the record and routed to recovery or reporting.
- `validate` reports mechanical checks (status, exit code, evidence presence) plus the `completion_criteria` checklist; **semantic verification remains OA's judgment**.
- Never approve success from `exit_code=0` alone. Validate `completion_criteria`, `evidence` (commands run, tests passed/failed), `artifacts`, and actual test results.
- Do not rewrite failed validation as success. A failed or unverifiable result goes back through recovery ([recover-maintain.md](recover-maintain.md)) or is reported honestly.
- `workflow-status` aggregates ledger state per workflow; use it before publishing `depends_on` successors.
