# OA Reference — Recovery, Control, and Maintenance

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Recovery

```bash
python "<PAO_SKILL>/scripts/oa.py" recover
python "<PAO_SKILL>/scripts/oa.py" dead
python "<PAO_SKILL>/scripts/oa.py" dead --lwar-id LWAR1 --requeue TASK_ID
```

- `recover` returns claimed tasks with expired leases to `incoming`, incrementing `attempt`; when `attempt` exceeds `max_retries`, the task is dead-lettered into `dead/` instead of looping forever.
- Each recovery writes an `interruption` record (`status: interrupted`, `recorded_by: oa_reconciler`) into the task's ledger entry: a vanished LWAR is recorded as interrupted, never inferred as success.
- `recover` also reconciles rejected tasks parked in `failed/` (claim-guard authority violations, schema rejections): their non-terminal ledger entries transition to `failed` with the rejection reason, so nothing sits at `published` forever.
- `dead --requeue` republishes a dead task with `attempt` **incremented** (never reset — attempt is the collect-side fencing key and must stay monotonic). A requeued dead task gets one execution chance per explicit decision; a further lease expiry dead-letters it again. `dead/` is never pruned automatically.
- **A `blocked` result is a task-definition failure, not a transient one — do NOT blind-requeue it.** When an LWAR returns `blocked` (unsatisfiable criteria, or authority the TaskContract never granted), re-queuing the identical task just burns the retry budget to `dead/` with the same outcome. Re-plan first: fix the `completion_criteria`, widen `permissions`, or correct the `cwd`/contract, then publish a **new** task (`send`) — or report honestly if it cannot be satisfied. `recover`/`dead --requeue` are for interrupted/crashed work, not for wrong contracts.

## Control

```bash
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command ping
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command drain
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command cancel --task-id TASK_ID
python "<PAO_SKILL>/scripts/oa.py" control --lwar-id LWAR1 --command shutdown
```

- `shutdown` requests ADP termination only. Deregistration is handled separately through lifecycle requests and `reconcile`.

## Maintenance

```bash
python "<PAO_SKILL>/scripts/oa.py" prune --older-than-days 14
```

- `prune` removes archived tasks/results/control, `failed/`, and `quarantine/` files older than the cutoff; it never touches `dead/`.
- Every OA, LWAR, and ADP action is mirrored to the append-only audit log at `var/audit/events.jsonl`.
