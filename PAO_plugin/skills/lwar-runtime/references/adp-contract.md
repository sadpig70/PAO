# ADP Runtime Contract v1.1

## Definition

**ADP — Agent Daemon Process** is the resident control loop inside an LWAR session. The Python watcher is a deterministic I/O tool; the actual repeating actor is the LWAR agent itself.

```text
Watch(≤90s) → stdout event → Agent decision
    idle/state_wait ────────────────┐
    task → execute → submit result ─┤→ Watch
    control → handle ───────────────┘
    shutdown → stop
```

## Mailbox

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
    archive/
        tasks/
        results/
        control/
    failed/
    dead/              # tasks whose retry budget is exhausted (OA managed)
    quarantine/        # stale or duplicate results isolated by OA collect
```

All writes use the sequence: temporary file → flush/fsync → `os.replace`. Task receipt is finalized by the atomic move `incoming → claimed`.

## Exit Codes

| Code | Event | Agent action |
|---:|---|---|
| `0` | `task_received` | Execute the task |
| `10` | `idle_timeout`, `state_wait` | Re-run the watcher immediately |
| `20` | `control` | Process the command |
| `30` | `adp_error` | Report, then stop |
| any other | any unknown | **Fail closed**: stop this slice, report `protocol_error`, never retry blindly |

The agent must inspect both the exit code and the stdout JSON `event`. When the
slot is expected to stay non-`on` for a while, pass `--state-wait-backoff-max
SECONDS` so the in-slice poll interval doubles up to that cap (resets on `on`).

## Result Contract

`status` is one of `succeeded`, `failed`, `blocked`, `cancelled`, `interrupted`,
`timed_out`, `protocol_error` — every outcome is submitted, never silently
dropped, and only `status=succeeded` can be accepted by OA validation. The
submission tool echoes `attempt` and `claim_token` into the result from the
claimed task file automatically — never set them in the draft. Pass
`--result-file` (and `--identity-file`) as **absolute paths**: they resolve
against the process working directory, not the bus root.

## Lease Alignment

When a task is claimed, the watcher extends the lease to cover the task's own
execution budget: `effective_lease_s = max(--lease-seconds, timeout_s + 30)`.
Long-running tasks therefore do not lose their lease mid-execution.

## Failure Recovery

- If the watcher exits, the LWAR session can invoke the same command again.
- If the LWAR session dies, the heartbeat becomes stale.
- OA `recover` returns claimed tasks with expired leases back to `incoming`,
  incrementing `attempt` and recording an `interruption` entry in the ledger;
  when `attempt` exceeds `max_retries` the task is moved to `dead/` instead of
  being requeued. `attempt` is monotonic (manual dead-requeue also increments).
- If a result already exists for the same `task_id`, do not auto-approve a replayed execution.
  OA `collect` quarantines duplicate, stale-generation, and stale-attempt results automatically.
- Even when a numeric slot is reused, messages with mismatched `generation` or `instance_id` must be rejected by the watcher.
