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
  --result-file "<BUS_ROOT>/mailbox/LWARn/work/TASK_ID/result.json"
```

`--result-file` and `--identity-file` resolve against the **process working directory**, not the bus root — pass absolute paths unless your working directory is the bus root.

Artifacts: declare them as path strings (relative paths resolve against the task `cwd`). The tool enforces that each declared artifact exists as a regular file inside the task `cwd` or a `permissions.write` root (and under `permissions.max_artifact_bytes` when set), then snapshots it into the content-addressed store `var/artifacts/<sha256>` and rewrites the entry as `{path, sha256, size_bytes, snapshot}` — never fabricate these fields yourself. OA verification checks the immutable snapshot, so changing the workspace file after submission is harmless. Tasks published by pre-0.6 OAs (no declared write roots) get a warning passthrough instead of a bounds failure.

After confirming `event=result_submitted`, return to the watcher immediately (see [adp-loop.md](adp-loop.md)).
