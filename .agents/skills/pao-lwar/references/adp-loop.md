# LWAR Reference — ADP Watch Loop Contract

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0). Read this document in full before the first watch slice.

Fresh registration enters this loop through `lwar.py response REQUEST_ID
--resident`. That command publishes a matching `starting` heartbeat and invokes
the watcher in the same Python process; its first `watching`, `running`, or
`idle` heartbeat completes startup without an agent scheduling boundary. Use
the standalone resident command below only to resume an already trusted identity
or after handling a delivered event.

## Core loop

```python
def ADP(identity_file: Path) -> None:
    while True:
        event = run(
            'python "<PAO_SKILL>/scripts/adp_watch.py" --identity-file',
            identity_file,
            '--resident',
        )
        if event.event in {"idle_timeout", "state_wait"}:
            continue
        if event.event == "adp_error":
            report_error_and_stop(event)
            return                                 # fatal terminator — stop ADP
        if event.event == "control":
            if event.command == "retire":
                submit_terminal_result_if_holding_a_claim()
                while run_lwar_retire(identity_file) != "lwar_retired":
                    observe_oa_status(identity_file)
                    wait_for_oa_reconcile_without_stopping()
                return
            if event.command == "shutdown":
                # If a task is still claimed, submit its terminal result FIRST
                # (§1.5) — a shutdown that arrives via an interleaved slice mid-
                # task must not drop the claim silently.
                submit_terminal_result_if_holding_a_claim()
                return
            handle_control(event)
            continue
        if event.event == "task_received":
            result = AI_execute_task(event.task)   # see execute-complete.md
            write_result_draft(result)
            run_lwar_complete(identity_file, event.task_id, result.file)
            continue
        # Any other / unknown event: fail closed on the SLICE, not the daemon.
        if holding_a_claim():
            submit_protocol_error_result()
        continue                                   # run the next slice; do NOT stop ADP

    # acceptance_criteria:
    #   - ADP is a daemon: the loop stays resident and never exits on its own
    #   - the loop is never terminated by elapsed time, iteration/slice count, or
    #     the agent's own judgment that it is "done" — only by control:shutdown,
    #     successful control:retire, a fatal adp_error, or context exhaustion
    #   - idle slice boundaries are crossed inside the resident watcher, which
    #     keeps polling and refreshing heartbeat without an agent turn
    #   - between task receipt and result submission, no second task is claimed
    #   - succeeded, failed, and blocked outcomes are all submitted as ResultContract payloads
```

Default watcher invocation:

```bash
python "<PAO_SKILL>/scripts/adp_watch.py" \
  --identity-file IDENTITY_FILE \
  --interval 5 \
  --timeout 90 \
  --lease-seconds 180 \
  --resident
```

`--timeout` remains the internal slice/heartbeat checkpoint in resident mode;
it no longer returns `idle_timeout`. The process returns only when it delivers a
task/control event or encounters a fatal error. This keeps a live LWAR session
observable even when the surrounding agent runtime is slow to schedule another
turn.

The adopted identity stores its canonical `bus_root`, so this identity-only
invocation is safe even when the bus is not `<cwd>/.pao`. A supplied `--root` or
`PAO_ROOT` must match the identity; a mismatch emits fatal `adp_error` without
touching the conflicting bus. Legacy identities still self-locate when stored at
their canonical `<root>/var/identities/` path.

## Exit codes and stdout events

The agent must inspect both the exit code and the stdout JSON `event`.

| Code | `event` | Immediate action |
|---:|---|---|
| `0` | `task_received` | Save `identity_file`, execute the task, then submit the result |
| `10` | `idle_timeout`, `state_wait` | Compatibility single-slice mode only (`--resident` omitted); re-run immediately |
| `20` | `control:ping` | Re-run the watcher |
| `20` | `control:drain` | Finish current work, then request lifecycle `draining` (read [lifecycle.md](lifecycle.md) first) and **keep watching** until `shutdown` |
| `20` | `control:cancel` | Stop the task if you already hold it and submit a `cancelled` result. A cancel for a task you have **not** claimed yet needs no memory: the watcher has already written a tombstone (see below) that auto-cancels the task deterministically whenever it is claimed |
| `20` | `control:retire` | Submit any held task's terminal result, then repeatedly run `lwar.py retire --identity-file IDENTITY_FILE`. Exit `2` means a lifecycle step is pending: inspect `oa-status` and keep waiting for OA `reconcile`. Stop ADP only on exit `0` / `lwar_retired`; exit `4` means an active claim must be completed first |
| `20` | `control:shutdown` | If you currently hold a claimed task, submit its terminal result first — `interrupted` (no verdict reached), or the `failed`/`blocked` verdict if you already have one; never invent `blocked` just because shutdown arrived. Then stop ADP |
| `30` | `adp_error` | Report the error, then stop ADP (this is the fatal terminator) |
| any other | any unknown event | **Fail closed on the SLICE, not the daemon**: end only the current slice; if a task is claimed, submit a `protocol_error` terminal result for it; then run the **next** watch slice. Never retry the unknown event blindly, and never treat it as a reason to terminate ADP — only the four terminators in SKILL Rule 3 do that |

Every watcher event includes the absolute `identity_file`. Heartbeats are written
by the watcher itself on every poll—the agent never emits or edits them.

Error discipline. `adp_error` (exit 30) means the watcher itself hit a fatal condition (e.g. the identity no longer verifies) and exited: **stop this ADP run and report** — do not blindly re-invoke the same command. The only case for a bounded retry is a *transient* error you have reason to believe is self-clearing (e.g. a momentary file lock); if you choose to retry, cap it at **3 consecutive identical `adp_error`s**, then stop and escalate to OA. Never loop on an unresolved error.

Cancel reaching an agent mid-execution. A `control:cancel` is delivered only through a watcher slice, but while you execute a claimed task you are not in the watcher. To notice a cancel for the task you currently hold, **interleave compatibility single-slice watcher calls without `--resident`** (use a low `--timeout`) into any long/blocking work: `claim_control` runs before `claim_task` inside the watcher, so a slice run while you already hold a claim surfaces the pending `control:cancel` without any risk of double-claiming (your `incoming` is empty). On seeing it, stop the task and submit a `cancelled` result. (A cancel for a task you have not yet claimed needs no such polling — the tombstone handles it, see below.)

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

- The resident watcher does not exit on idle; if it exits after delivering a task or non-terminal control, re-invoke the same resident command immediately.
- A `starting` heartbeat older than 30 seconds means the atomic in-process
  watcher entry failed or stalled; report `adp_error` evidence rather than
  treating agent latency as an acceptable cause.
- If the LWAR session dies, the heartbeat goes stale; OA `recover` returns expired-lease tasks to `incoming`.
- If a result already exists for the same `task_id`, do not auto-approve a replayed execution; OA `collect` quarantines duplicate and stale-generation results.
- Even when a numeric slot is reused, messages with mismatched `generation` or `instance_id` must be rejected.

## Cancel tombstones

Cancelling a task that has not been claimed yet is deterministic and no longer depends on agent memory across watch slices:

- When the watcher claims a `cancel` control carrying a `task_id`, it writes a tombstone at `mailbox/LWARn/cancelled/{task_id}.json` **before** the `control:cancel` event is returned to the agent.
- On any later claim, a task whose `task_id` is tombstoned is not handed to the agent. The watcher submits a terminal `cancelled` result through the normal pipeline — `attempt` and `claim_token` echoed from the claimed task, the summary naming the tombstone — consumes the tombstone, and keeps scanning. No new agent-visible event is emitted; the agent contract is unchanged.
- The tombstone makes the not-yet-claimed cancel race-free even if the cancel control and the task publish arrive in either order. Duplicate cancels are first-writer-wins, and a tombstone for an already-completed (or never-arriving) task is simply never consumed — both are harmless. The `control:cancel` event still reaches the agent so it can stop a task it is already executing.
