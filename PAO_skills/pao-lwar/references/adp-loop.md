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
| `20` | `control:cancel` | Stop the task if you already hold it and submit a `cancelled` result. A cancel for a task you have **not** claimed yet needs no memory: the watcher has already written a tombstone (see below) that auto-cancels the task deterministically whenever it is claimed |
| `20` | `control:shutdown` | Stop ADP |
| `30` | `adp_error` | Report the error, then stop ADP |
| any other | any unknown event | **Fail closed**: stop this slice, report `protocol_error`, never retry an unknown event blindly |

Heartbeats are written by the watcher itself on every poll — the agent never emits or edits them.

Error discipline. `adp_error` (exit 30) means the watcher itself hit a fatal condition (e.g. the identity no longer verifies) and exited: **stop this ADP run and report** — do not blindly re-invoke the same command. The only case for a bounded retry is a *transient* error you have reason to believe is self-clearing (e.g. a momentary file lock); if you choose to retry, cap it at **3 consecutive identical `adp_error`s**, then stop and escalate to OA. Never loop on an unresolved error.

Cancel reaching an agent mid-execution. A `control:cancel` is delivered only through a watcher slice, but while you execute a claimed task you are not in the watcher. To notice a cancel for the task you currently hold, **interleave short watcher slices** (e.g. a low `--timeout`) into any long/blocking work: `claim_control` runs before `claim_task` inside the watcher, so a slice run while you already hold a claim surfaces the pending `control:cancel` without any risk of double-claiming (your `incoming` is empty). On seeing it, stop the task and submit a `cancelled` result. (A cancel for a task you have not yet claimed needs no such polling — the tombstone handles it, see below.)

When the slot is expected to stay in a non-`on` state for a while (e.g. `draining` wind-down), pass `--state-wait-backoff-max SECONDS` so the in-slice poll interval doubles up to that cap instead of busy-polling at `--interval`; it resets automatically when the state returns to `on`.

## Mailbox layout

```text
mailbox/LWARn/
    incoming/          # OA task publish area
    claimed/           # task atomically claimed by ADP
    outgoing/          # LWAR result publish area
    control/           # OA control publish area
    control_claimed/   # transient watcher claim area
    cancelled/         # cancel tombstones ({task_id}.json)
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

## Cancel tombstones

Cancelling a task that has not been claimed yet is deterministic and no longer depends on agent memory across watch slices:

- When the watcher claims a `cancel` control carrying a `task_id`, it writes a tombstone at `mailbox/LWARn/cancelled/{task_id}.json` **before** the `control:cancel` event is returned to the agent.
- On any later claim, a task whose `task_id` is tombstoned is not handed to the agent. The watcher submits a terminal `cancelled` result through the normal pipeline — `attempt` and `claim_token` echoed from the claimed task, the summary naming the tombstone — consumes the tombstone, and keeps scanning. No new agent-visible event is emitted; the agent contract is unchanged.
- The tombstone makes the not-yet-claimed cancel race-free even if the cancel control and the task publish arrive in either order. Duplicate cancels are first-writer-wins, and a tombstone for an already-completed (or never-arriving) task is simply never consumed — both are harmless. The `control:cancel` event still reaches the agent so it can stop a task it is already executing.
