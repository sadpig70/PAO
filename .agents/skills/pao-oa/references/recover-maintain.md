# OA Reference — Recovery, Control, and Maintenance

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Recovery

```bash
python "<PAO_SKILL>/scripts/oa.py" recover --delivery-timeout 300
python "<PAO_SKILL>/scripts/oa.py" dead
python "<PAO_SKILL>/scripts/oa.py" dead --lwar-id LWAR1 --requeue TASK_ID
```

- `recover` returns claimed tasks with expired leases to `incoming`, incrementing `attempt`; when `attempt` exceeds `max_retries`, the task is dead-lettered into `dead/` instead of looping forever. It also dead-letters unclaimed `incoming` deliveries older than `--delivery-timeout`.
- Recovery uses durable transitional states (`requeueing`, `dead_lettering`) before mailbox moves and repairs interrupted publication/manual-requeue/archive commits on the next pass.
- Each recovery writes an `interruption` record (`status: interrupted`, `recorded_by: oa_reconciler`) into the task's ledger entry: a vanished LWAR is recorded as interrupted, never inferred as success.
- `recover` also reconciles rejected tasks parked in `failed/` (claim-guard authority violations, schema rejections): their non-terminal ledger entries transition to `failed` with the rejection reason, so nothing sits at `published` forever.
- `dead --requeue` republishes a dead task with `attempt` **incremented** (never reset — attempt is the collect-side fencing key and must stay monotonic). A requeued dead task gets one execution chance per explicit decision; a further lease expiry dead-letters it again. `dead/` is never pruned automatically.
- **A `blocked` result is a task-definition failure, not a transient one — do NOT blind-requeue it.** When an LWAR returns `blocked` (unsatisfiable criteria, or authority the TaskContract never granted), re-queuing the identical task just burns the retry budget to `dead/` with the same outcome. Re-plan first: fix the `completion_criteria`, widen `permissions`, or correct the `cwd`/contract, then publish a **new** task (`send`) — or report honestly if it cannot be satisfied. `recover`/`dead --requeue` are for interrupted/crashed work, not for wrong contracts.

## Control

```bash
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command ping
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command drain
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command cancel --task-id TASK_ID
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command retire
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command shutdown
```

- Control delivery is at-least-once: the watcher keeps a claimed control until it acknowledges the emitted event. Handlers must therefore be idempotent.
- `shutdown` requests a resumable ADP stop and deliberately retains the slot.
- `retire` requests a clean one-time shutdown: LWAR submits any terminal result,
  then repeatedly drives `on → draining → off → deregistered` with `lwar.py
  retire`. OA must keep presence live and run `reconcile` until the LWAR reports
  `lwar_retired`; only then is the numeric slot returned.

## Maintenance

```bash
python "<PAO_SKILL>/scripts/oa.py" prune --older-than-days 14
```

- `prune` removes old archives, failed/quarantined files, consumed-or-orphan tombstones, unreferenced artifact snapshots, and rotated audit segments. It preserves active tombstones, ledger-referenced artifacts, the active audit log, and `dead/`.
- Audit append failures degrade observability only; they are reported on stderr and never cancel an already-valid control/task/result operation. The active audit log rotates at a bounded segment size.
