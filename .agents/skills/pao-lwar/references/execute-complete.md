# LWAR Reference — Task Execution and Result Submission

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Execution rules

- Inspect `cwd`, `permissions`, and `completion_criteria` first.
- When a criterion says content must be "exactly" some value, produce it byte-exact with no trailing newline unless the task states otherwise, and record the exact bytes written in `evidence`.
- Do not use paths, commands, or network access that the task does not allow.
- Perform exact verification through real commands and code, and record evidence under `evidence`.
- **Emit `status=succeeded` only when every `completion_criterion` has been independently verified** by a real command/check whose evidence you recorded. If any criterion is unmet or you cannot verify it within the task's authority, emit `blocked` (unsatisfiable/insufficient authority) or `failed` — never optimistic `succeeded`. `exit_code=0` from a build/test is not by itself success; the OA re-checks, and an unverified `succeeded` is a protocol violation on this side.
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

Choose the status by situation — do not guess:

| Situation | `status` |
|---|---|
| every completion_criterion verified | `succeeded` |
| the work ran but a criterion is unmet / a test failed | `failed` |
| the task is unsatisfiable as written, or needs authority the TaskContract does not grant (a contradictory/under-scoped contract) | `blocked` |
| you saw a `control:cancel` for this task while executing it | `cancelled` |
| your own execution overran the task's `timeout_s` | `timed_out` |
| the task payload or an event was malformed / a protocol invariant broke | `protocol_error` |
| you must stop mid-task without a `failed`/`blocked` verdict (context-exhaustion handoff, or a `control:shutdown` arrived mid-execution) | `interrupted` |

`interrupted` is also written by OA's reconciler for a vanished LWAR (lease expiry); either origin is legitimate. When two apply (e.g. a cancel during an overrun), prefer the more specific cause (`cancelled` over `timed_out`).

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

Handling a `complete` that does not report `result_submitted`:

- **Claim superseded** (the lease expired and OA re-queued the task): do **not** retry the submission — the re-queued attempt is canonical. Return to the watcher.
- **Draft rejected** (a schema/field error, an artifact outside the allowed write roots, an artifact-bounds failure): the error names the cause. Fix the draft once — correct the field or drop/relocate the offending artifact — and resubmit a single time.
- **Any other failure** (I/O error, the identity/result file unreadable): do not silently drop the claim. Fix the cause if you can and resubmit once; if it still fails, keep the claim (let its lease expire so OA `recover` reclaims it) and report the blocker — never abandon a claim with no terminal result and no recovery path.

After confirming `event=result_submitted`, return to the watcher immediately (see [adp-loop.md](adp-loop.md)).
