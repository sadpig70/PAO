# LWAR Reference — Task Execution and Result Submission

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Execution rules

- Inspect `cwd`, `permissions`, and `completion_criteria` first.
- When a criterion says content must be "exactly" some value, produce it byte-exact with no trailing newline unless the task states otherwise, and record the exact bytes written in `evidence`.
- Do not use paths, commands, or network access that the task does not allow.
- Perform exact verification through real commands and code, and record evidence under `evidence`.
- Write the draft result to `mailbox/LWARn/work/{task_id}/result.json` (path relative to the bus root).

## Draft result format

```json
{
  "status": "succeeded",
  "summary": "Task summary",
  "evidence": {"commands": [], "tests_passed": 0, "tests_failed": 0},
  "artifacts": [],
  "next_action": "validate",
  "exit_code": 0,
  "error": null
}
```

`status` is one of `succeeded`, `failed`, `blocked`, `cancelled`, `interrupted`, `timed_out`, `protocol_error` — all outcomes are submitted, never silently dropped. `complete` is a terminal submission, **not** a success claim: only `status=succeeded` can ever be accepted by OA validation, and exactly one terminal result is submitted per claim.

The submission tool echoes `attempt` and `claim_token` into the result from the claimed task file automatically — never set them in the draft. If `complete` reports that the claim was superseded (the lease expired and OA re-queued the task), do not retry the submission; return to the watcher.

## Submission

```bash
python "<PAO_SKILL>/scripts/lwar.py" complete \
  --identity-file IDENTITY_FILE \
  --task-id TASK_ID \
  --result-file mailbox/LWARn/work/TASK_ID/result.json
```

After confirming `event=result_submitted`, return to the watcher immediately (see [adp-loop.md](adp-loop.md)).
