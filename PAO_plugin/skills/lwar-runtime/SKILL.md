---
name: lwar-runtime
description: "PAO LWAR self-registration and ADP (Agent Daemon Process) resident loop contract. Load on /lwar-register, /lwar-status, /lwar-on, /lwar-drain, /lwar-off, /lwar-unregister, or whenever an assigned LWAR must watch its mailbox and execute OA tasks."
user-invocable: true
argument-hint: "register [number] | adp | status | on | drain | off | unregister"
---

# LWAR Runtime Skill v2 — ADP

> ADP is the **Agent Daemon Process**. An already-running LWAR session repeatedly invokes a Python watcher, receives its mailbox, performs work, stores a result, and returns to the watcher.

> **Bus root resolution**: every command resolves the bus as explicit `--root` > `PAO_ROOT` environment variable > current directory. In operation mode (LWAR session in a project workspace), set `PAO_ROOT` to the central bus and omit `--root`. Task execution still happens in each task's own `cwd`.
>
> **Installation root**: every example below uses `python "$PAO_HOME/scripts/..."`, where `$PAO_HOME` is the PAO installation directory. Resolve it in this order: (1) **Claude Code plugin install** — the directory is: `${CLAUDE_PLUGIN_ROOT}`; when this skill is loaded from the installed plugin, that token is already substituted with the absolute plugin path — use it in place of `$PAO_HOME`; if it appears unsubstituted, fall back to the next rule. (2) **Manual / foreign-runtime install** — set the `PAO_HOME` environment variable to the PAO repository path. (3) **Inside the PAO repository** — the repository's `PAO_plugin/` directory; `python -m pao_runtime.*` (from inside `PAO_plugin/`) and the optional pip console scripts (`pao-lwar` / `pao-adp-watch`) remain equivalent alternatives there. The wrappers bootstrap their own import path; no pip install is required in any mode.

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

When PAO is installed as a Claude Code plugin, these commands carry the plugin namespace: `/pao:lwar-register`, `/pao:lwar-adp`, `/pao:lwar-status`, `/pao:lwar-on`, `/pao:lwar-drain`, `/pao:lwar-off`, `/pao:lwar-unregister`. The bare forms remain the contract for runtimes that load this skill directly.

## 3. Registration

Use only actual runtime metadata. Do not guess unknown values.

```bash
python "$PAO_HOME/scripts/lwar.py" register \
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
python "$PAO_HOME/scripts/lwar.py" response REQUEST_ID
```

When `event=identity_adopted`, the printed `identity_file` becomes the only valid identity input for later ADP calls. If the response is `pending`, do not treat the identity as approved.

## 4. Core ADP Loop

```python
def ADP(identity_file: Path) -> None:
    while True:
        event = run('python "$PAO_HOME/scripts/adp_watch.py" --identity-file', identity_file)
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
python "$PAO_HOME/scripts/adp_watch.py" \
  --identity-file IDENTITY_FILE \
  --interval 5 \
  --timeout 90 \
  --lease-seconds 180
```

When a task is claimed, the watcher aligns the lease with the task budget:
`effective_lease_s = max(--lease-seconds, timeout_s + 30)`. Long tasks keep
their lease for the whole declared execution window.

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
python "$PAO_HOME/scripts/lwar.py" complete \
  --identity-file IDENTITY_FILE \
  --task-id TASK_ID \
  --result-file mailbox/LWARn/work/TASK_ID/result.json
```

After confirming `event=result_submitted`, re-run the watcher.

## 7. Lifecycle

```bash
python "$PAO_HOME/scripts/lwar.py" state draining --identity-file IDENTITY_FILE
python "$PAO_HOME/scripts/lwar.py" state off --identity-file IDENTITY_FILE
python "$PAO_HOME/scripts/lwar.py" state on --identity-file IDENTITY_FILE
python "$PAO_HOME/scripts/lwar.py" state deregistered --identity-file IDENTITY_FILE
```

Do not assume the state is final until OA reconciles it and `/lwar-status` confirms it. Request `deregistered` only from `off`.

## 8. Forbidden Actions

- Do not claim an `LWARn` identity before approval.
- Do not modify registry, incoming, or lease files by hand.
- Do not pollute context by restating idle stdout messages at length.
- Do not abandon a claimed task without a result.
- Do not stop ADP on your own without a user or OA `shutdown`.
