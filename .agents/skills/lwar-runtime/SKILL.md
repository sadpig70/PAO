---
name: lwar-runtime
description: "PAO LWAR self-registration and ADP (Agent Daemon Process) resident loop contract. Load on /lwar-register, /lwar-status, /lwar-on, /lwar-drain, /lwar-off, /lwar-unregister, or whenever an assigned LWAR must watch its mailbox and execute OA tasks."
user-invocable: true
argument-hint: "register [number] | adp | status | on | drain | off | unregister"
---

# LWAR Runtime Skill v2 — ADP

> ADP is the **Agent Daemon Process**. An already-running LWAR session repeatedly invokes a Python watcher, receives its mailbox, performs work, stores a result, and returns to the watcher.

## 1. Absolute Rules

1. Read this skill and [`references/adp-contract.md`](references/adp-contract.md) in full.
2. Use only the approved `(lwar_id, instance_id, generation)` as your runtime identity.
3. Do not assume an external process will relaunch the LWAR. Keep ADP alive inside the current session.
4. On `idle_timeout` and `state_wait`, generate no extra explanation. Re-run the same watcher immediately.
5. On `task_received`, operate only within the TaskContract authority bounds and always submit a result with `complete`.
6. Return to the watcher immediately after result submission.
7. Only `shutdown` terminates ADP.

## 2. Command Contract

| User command | Action |
|---|---|
| `/lwar-register` | Request automatic slot registration |
| `/lwar-register 5` | Request the `LWAR5` slot |
| `/lwar-adp` | Start ADP with an approved identity |
| `/lwar-status` | Inspect registry and heartbeat |
| `/lwar-on` | Request transition to `on` |
| `/lwar-drain` | Request transition to `draining` |
| `/lwar-off` | Request transition to `off` |
| `/lwar-unregister` | Request `deregistered` after `off` |

`/lwar-regite` remains an accepted typo alias for `/lwar-register`.

## 3. Registration

Use only actual runtime metadata. Do not guess unknown values.

```bash
python -m pao_runtime.lwar_cli register \
  --runtime-name "Codex" \
  --model "GPT 5.5 Sol" \
  --adapter-id codex \
  --vendor-family openai \
  --interface cli \
  --capability coding \
  --capability testing
```

Remember the `request_id` returned on stdout. After OA approves it, run:

```bash
python -m pao_runtime.lwar_cli response REQUEST_ID
```

When `event=identity_adopted`, the printed `identity_file` becomes the only valid identity input for later ADP calls. If the response is `pending`, do not treat the identity as approved.

## 4. Core ADP Loop

```python
def ADP(identity_file: Path) -> None:
    while True:
        event = run("python -m pao_runtime.adp_watch --identity-file", identity_file)
        if event.event in {"idle_timeout", "state_wait"}:
            continue
        if event.event == "adp_error":
            report_error_and_stop(event)
        if event.event == "control":
            if event.command == "shutdown":
                return
            handle_control(event)
            continue
        if event.event == "task_received":
            result = AI_execute_task(event.task)
            write_result_draft(result)
            run_lwar_complete(identity_file, event.task_id, result.file)
            continue

    # acceptance_criteria:
    #   - watcher timeout does not terminate the LWAR session.
    #   - after timeout, the watcher is re-run without extra reasoning.
    #   - between task receipt and result submission, no second task is claimed.
    #   - succeeded, failed, and blocked outcomes are all submitted as ResultContract payloads.
```

Default watcher invocation:

```bash
python -m pao_runtime.adp_watch \
  --identity-file IDENTITY_FILE \
  --interval 1 \
  --timeout 90 \
  --lease-seconds 180
```

## 5. Stdout Event Handling

| `event` | Immediate action |
|---|---|
| `idle_timeout` | Re-run the same watcher |
| `state_wait` | Re-run the same watcher; do not execute tasks |
| `task_received` | Execute the `task`, then submit the result |
| `control:ping` | Re-run the watcher |
| `control:drain` | Finish current work, then request lifecycle `draining` |
| `control:cancel` | Stop that task and submit a `cancelled` result |
| `control:shutdown` | Stop ADP |
| `adp_error` | Report the error, then stop ADP |

## 6. Task Execution and Result Submission

- Inspect `cwd`, `permissions`, and `completion_criteria` first.
- Do not use paths, commands, or network access that the task does not allow.
- Perform exact verification through real commands and code, and record evidence under `evidence`.
- Write the draft result to `mailbox/LWARn/work/{task_id}/result.json`.

Draft result format:

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

Submit with:

```bash
python -m pao_runtime.lwar_cli complete \
  --identity-file IDENTITY_FILE \
  --task-id TASK_ID \
  --result-file mailbox/LWARn/work/TASK_ID/result.json
```

After confirming `event=result_submitted`, re-run the watcher.

## 7. Lifecycle

```bash
python -m pao_runtime.lwar_cli state draining --identity-file IDENTITY_FILE
python -m pao_runtime.lwar_cli state off --identity-file IDENTITY_FILE
python -m pao_runtime.lwar_cli state on --identity-file IDENTITY_FILE
python -m pao_runtime.lwar_cli state deregistered --identity-file IDENTITY_FILE
```

Do not assume the state is final until OA reconciles it and `/lwar-status` confirms it. Request `deregistered` only from `off`.

## 8. Forbidden Actions

- Do not claim an `LWARn` identity before approval.
- Do not modify registry, incoming, or lease files by hand.
- Do not pollute context by restating idle stdout messages at length.
- Do not abandon a claimed task without a result.
- Do not stop ADP on your own without a user or OA `shutdown`.
