# LWAR Reference — ADP Watch Loop Contract

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0). Read this document in full before the first watch slice.

## Core loop

```python
def ADP(identity_file: Path) -> None:
    while True:
        event = run('python "<PAO_SKILL>/scripts/adp_watch.py" --identity-file', identity_file)
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
            result = AI_execute_task(event.task)   # see execute-complete.md
            write_result_draft(result)
            run_lwar_complete(identity_file, event.task_id, result.file)
            continue

    # acceptance_criteria:
    #   - watcher timeout does not terminate the LWAR session
    #   - after timeout, the watcher is re-run without extra reasoning
    #   - between task receipt and result submission, no second task is claimed
    #   - succeeded, failed, and blocked outcomes are all submitted as ResultContract payloads
```

Default watcher invocation:

```bash
python "<PAO_SKILL>/scripts/adp_watch.py" \
  --identity-file IDENTITY_FILE \
  --interval 5 \
  --timeout 90 \
  --lease-seconds 180
```

## Exit codes and stdout events

The agent must inspect both the exit code and the stdout JSON `event`.

| Code | `event` | Immediate action |
|---:|---|---|
| `0` | `task_received` | Execute the task, then submit the result |
| `10` | `idle_timeout`, `state_wait` | Re-run the same watcher immediately |
| `20` | `control:ping` | Re-run the watcher |
| `20` | `control:drain` | Finish current work, then request lifecycle `draining` (read [lifecycle.md](lifecycle.md) first) and **keep watching** until `shutdown` |
| `20` | `control:cancel` | Stop that task and submit a `cancelled` result |
| `20` | `control:shutdown` | Stop ADP |
| `30` | `adp_error` | Report the error, then stop ADP |
| any other | any unknown event | **Fail closed**: stop this slice, report `protocol_error`, never retry an unknown event blindly |

Heartbeats are written by the watcher itself on every poll — the agent never emits or edits them.

Error discipline across slices: the watcher itself never loops on an error (it exits the slice), but the agent must apply a cap — after 3 consecutive `adp_error` slices with the same error, stop re-running and escalate to OA instead of retrying forever.

When the slot is expected to stay in a non-`on` state for a while (e.g. `draining` wind-down), pass `--state-wait-backoff-max SECONDS` so the in-slice poll interval doubles up to that cap instead of busy-polling at `--interval`; it resets automatically when the state returns to `on`.

## Mailbox layout

```text
mailbox/LWARn/
    incoming/          # OA task publish area
    claimed/           # task atomically claimed by ADP
    outgoing/          # LWAR result publish area
    control/           # OA control publish area
    control_claimed/   # transient watcher claim area
    leases/            # execution leases
    work/              # LWAR working files
    heartbeat.json
    archive/  failed/  dead/  quarantine/
```

All writes use temporary file → flush/fsync → `os.replace`. Task receipt is finalized by the atomic move `incoming → claimed`.

## Lease alignment

When a task is claimed, the watcher extends the lease to cover the task's own execution budget: `effective_lease_s = max(--lease-seconds, timeout_s + 30)`. Long tasks keep their lease for the whole declared window.

## Failure recovery

- If the watcher exits, re-invoke the same command; the session survives watcher timeouts by design.
- If the LWAR session dies, the heartbeat goes stale; OA `recover` returns expired-lease tasks to `incoming`.
- If a result already exists for the same `task_id`, do not auto-approve a replayed execution; OA `collect` quarantines duplicate and stale-generation results.
- Even when a numeric slot is reused, messages with mismatched `generation` or `instance_id` must be rejected.
