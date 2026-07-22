# OA Reference — Recovery, Control, and Maintenance

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Recovery

```bash
python "<PAO_SKILL>/scripts/oa.py" recover --delivery-timeout 300
python "<PAO_SKILL>/scripts/oa.py" recover --reap-startup --lwar-id LWAR1 --instance-id INSTANCE_ID --generation GENERATION --startup-deadline 30
python "<PAO_SKILL>/scripts/oa.py" dead
python "<PAO_SKILL>/scripts/oa.py" dead --lwar-id LWAR1 --requeue TASK_ID
```

- `recover` returns claimed tasks with expired leases to `incoming`, incrementing `attempt`; when `attempt` exceeds `max_retries`, the task is dead-lettered into `dead/` instead of looping forever. It also dead-letters unclaimed `incoming` deliveries older than `--delivery-timeout`.
- `recover --reap-startup` is an explicit, fenced recovery for a matching
  `starting` heartbeat older than the startup deadline. Take all three target
  values from the same current `status` result. The runtime rechecks
  `lwar_id + instance_id + generation`, heartbeat status/age, and the mailbox
  under the registry lock. It refuses fresh, started, identity-mismatched, or
  work-bearing slots; active `incoming`, `claimed`, `leases`, `outgoing`,
  `control`, or `control_claimed` files are never discarded. Success records
  `startup_deadline_missed` and `startup_slot_reaped`, removes the slot, and
  writes a generation-preserving tombstone. A repeated identical command is
  idempotent (`reason=already_reaped`).
- Startup reaping commits the tombstone before deleting the registry slot. A
  crash between those writes leaves a conservative partial state: the old slot
  remains occupied and its generation is already fenced. After the dead
  command/registry locks become stale, retry the exact command; it completes
  the registry removal with one version increment. A crash after both writes is
  handled by the `already_reaped` replay path: registry and tombstone bytes stay
  unchanged, registry version does not increase, and the missing deadline/reap
  audit records are emitted once by the successful replay. Each accepted
  startup-reap audit has a deterministic key derived from
  `lwar_id + instance_id + generation + event`; a crash between the deadline
  and reap audit appends therefore replays only the missing event. Active,
  rotated, and degraded audit segments all participate in duplicate detection.
  Repeated active-log failures serialize through a separate degraded-spool lock
  and retain only one pending record per deterministic key; the first healthy
  append promotes that record once and removes the spool. If a process stops
  after flushing the active segment but before deleting the spool, retry skips
  keys already present in active or rotated segments before removing the spool.
  Active and degraded append paths call `fsync` after flush. The spool is never
  deleted until the active durability barrier succeeds; if it fails, the event
  is retained through the independently durable degraded path.
  Keyed append and replay require every active and rotated segment to be
  readable. A read or UTF-8 decode fault is not interpreted as key absence:
  active append stops, and the event remains in `degraded.jsonl` for retry.
  JSONL validation also rejects malformed lines and non-object values. A final
  malformed fragment without a newline is the only automatic repair case for
  mutable `events.jsonl` or `degraded.jsonl`: raw bytes are `fsync`-quarantined
  under `var/audit/.corrupt/` before the source is truncated. Malformed rotated,
  terminated, or interior lines remain unchanged and fail closed until an
  operator preserves the file and repairs the invalid line.
- Every mutating OA command holds the same command-wide process lock. Therefore
  a concurrent `send` cannot publish from a stale registry observation while
  startup reaping commits; one command completes before the other revalidates.
- A killed OA process can leave `.command.lock` behind. Never remove it by hand:
  the lock implementation checks its recorded PID on POSIX and Windows, keeps a
  live holder fenced even when the file is old, and reclaims a dead holder after
  the 30-second stale threshold.
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
python "<PAO_SKILL>/scripts/oa.py" audit-health
python "<PAO_SKILL>/scripts/oa.py" audit-repair --segment events.123.jsonl --expected-sha256 SHA256 --drop-line 7
python "<PAO_SKILL>/scripts/oa.py" prune --older-than-days 14
```

- `audit-health` is strictly read-only: it acquires no writer/audit lock and creates no lease, presence, audit, or repair file. It reports `healthy`, `attention`, or `blocked`, segment line/key/SHA-256 diagnostics, exact `repair_candidates`, `keyed_append_blocked`, `blocked_replay`, the degraded pending count, quarantine artifacts, and operator-safe guidance. Exit code `2` means blocked; other states return `0`. A repairable active/degraded tail is only diagnosed here — retrying the original guarded OA operation performs the bounded quarantine-before-truncate repair. Never delete a malformed segment to make health green.
- `audit-repair` is a mutating, operator-authorized recovery for corruption that
  cannot be auto-repaired. Use the exact segment name, its current SHA-256, and
  repeat `--drop-line` for every malformed 1-based line reported by the same
  diagnosis. The command rejects path traversal, fingerprint drift, duplicate
  or partial selections, and any attempt to remove a valid line. Under the OA
  writer guard and `.audit.lock` → `.degraded.lock`, it validates the candidate,
  durably preserves the original as `.corrupt/*.repair-original`, atomically
  replaces the segment, records a deterministic repair event, and triggers a
  healthy degraded replay. Exit `2` means another segment still blocks replay;
  `attention` after success is expected while preserved evidence remains.
- `prune` removes old archives, failed/quarantined files, consumed-or-orphan tombstones, unreferenced artifact snapshots, and rotated audit segments. Audit pruning holds `.audit.lock` then `.degraded.lock`, matching append order. A rotated segment whose deterministic key is also pending in `degraded.jsonl` is retained until healthy replay clears the spool; unreadable pending evidence fails closed. Active tombstones, ledger-referenced artifacts, the active audit log, and `dead/` are always preserved.
- Audit append failures degrade observability only; they are reported on stderr and never cancel an already-valid control/task/result operation. The active audit log rotates at a bounded segment size.
