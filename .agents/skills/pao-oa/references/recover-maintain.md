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
  A durable `.repairs/` receipt is written before replacement and advances
  through `prepared`, `replaced`, and `committed`. If the process stops at any
  boundary, repeat the exact command. The runtime resumes only when the receipt,
  backup, operator arguments, and current target digest agree; otherwise it
  fails closed. `audit-health` exposes receipt phase and pending receipt count.
- `prune` removes old archives, failed/quarantined files,
  consumed-or-orphan tombstones, unreferenced artifact snapshots, rotated audit
  segments, and fully committed repair evidence. Audit pruning holds
  `.audit.lock` then `.degraded.lock`, matching append order. A repair receipt
  and original backup are eligible only when the receipt is old and
  `committed`, its deterministic audit key is present, the backup still matches,
  and the target proves the repaired state or a consumed degraded spool.
  `prepared`, `replaced`, invalid, drifted, missing, and ambiguous evidence is
  retained. `repair_receipts_removed` and `repair_backups_removed` are reported
  separately. Before either removal, the runtime writes a strict
  `.repair-prune/` transaction tombstone. It then removes the receipt, atomically
  moves the matching backup into same-filesystem staging, records
  `backup_staged`, removes the staged backup, and removes the tombstone last.
  Repeating `prune` resumes any authorized transaction at every process-stop
  boundary, even with a different new cutoff. Malformed, conflicting, missing,
  or drifted transaction state remains untouched. A rotated segment whose
  deterministic key is also pending in
  `degraded.jsonl` is retained until healthy replay clears the spool; unreadable
  pending evidence fails closed. Active tombstones, ledger-referenced artifacts,
  the active audit log, and `dead/` are always preserved.
- `audit-health` inspects `.repair-prune/` strictly read-only. Each entry is
  `resumable` only when its schema, audit key, target, remaining evidence, and
  file topology exactly match a supported process-stop boundary. Otherwise it
  is `blocked` with stable `reason_codes`. The output includes
  `retention_tombstones`, `resumable_retention_count`, and
  `blocked_retention_count`. Either class raises overall status to `attention`;
  only audit-segment health controls `keyed_append_blocked`. Run `prune` for
  resumable entries. Never manually delete blocked evidence; restore its bound
  state first.
- Rotated pruning snapshots every retention tombstone before deleting any
  segment. While a tombstone exists, its named rotated repair target and every
  rotated segment carrying its deterministic repair audit key are protected,
  whether the transaction is resumable or blocked. One malformed, unreadable,
  or non-file tombstone makes the entire rotated prune pass fail closed with
  zero removals. The fences disappear only after retention completion removes
  the tombstone.
- Every age-eligible rotated segment that is not reference-protected must pass
  complete readable JSON-object JSONL validation before deletion. Malformed,
  non-object, unreadable, metadata-inaccessible, and unlink-failed segments
  remain untouched. `prune` reports `audit_segments_removed`,
  `audit_segments_protected`, and `audit_segments_blocked`; protected and
  blocked counts never inflate `total`. `audit_segment_outcomes` maps every
  counted segment to a bus-root-relative `path`, one matching status, and
  stable `reason_codes`. Use those codes for automated remediation routing,
  and the optional `error` only as human context. The list length equals the
  three counts combined and the identical list is retained in the `pruned`
  audit event.
- Rotated deletion is crash-convergent through one strict
  `.rotated-prune/<run_id>.json` receipt. The runtime durably records the
  cutoff, every outcome, and exact SHA-256/byte witnesses before deleting any
  `valid_expired` segment. After interruption, rerun `prune`: it resumes the
  pending run before creating another, treats an authorized absent file as
  already removed, verifies every remaining fingerprint, and reports drift as
  `segment_drifted` without deletion. The `pruned` event uses deterministic key
  `rotated-prune:<run_id>`. The receipt is deleted only after that key appears
  in a complete audit snapshot. If `audit_prune_audit_committed` is false,
  preserve the receipt and repair audit health before retrying.
- `audit-health` inspects `.rotated-prune/` without locks or writes and returns
  `rotated_prune_receipts`, `resumable_rotated_prune_count`, and
  `blocked_rotated_prune_count`. Matching or authorized-absent targets in valid
  `prepared`/`applied` receipts are resumable. Invalid schema, multiple or
  unexpected entries, unreadable/non-file targets, fingerprint drift,
  incomplete audit snapshots, or an `applied` deletion target that is present
  are reason-coded blocked. Run `prune` for resumable entries. Never delete a
  blocked receipt manually; restore the exact state identified by its
  `reason_codes`.
- For exactly one `applied_target_present` receipt, take one maintenance
  snapshot and calculate the current receipt and recreated-segment SHA-256
  values. Then run:

  ```bash
  python "<PAO_SKILL>/scripts/oa.py" audit-prune-resolve \
    --run-id <RUN_ID> \
    --expected-receipt-sha256 <RECEIPT_SHA256> \
    --segment events.<N>.jsonl \
    --expected-segment-sha256 <SEGMENT_SHA256> \
    --decision preserve-recreated \
    --root <BUS_ROOT>
  ```

  The command refuses invalid or multiple receipts and all fence drift. It
  never deletes the recreated segment. It durably writes a
  `.rotated-preserve/` marker, reason-codes the retained outcome, recovers the
  original prune event, and records the operator decision exactly once before
  completing the receipt. Retry the exact command after interruption. Future
  `prune` calls retain the target as `operator_preserved_target`.
- `audit-health` also snapshots `.rotated-preserve/` strictly read-only and
  returns `rotated_preservations`,
  `protected_rotated_preservation_count`, and
  `blocked_rotated_preservation_count`. A marker is `protected` only when its
  target matches the recorded SHA-256/byte count and both
  `rotated-prune:<run_id>` and the marker's resolution key are committed.
  `orphaned_marker`, `target_fingerprint_drift`, `duplicate_target_claim`,
  invalid entries, and missing audit bindings are stable blocked reasons.
  These states raise overall health to `attention` but do not change
  `keyed_append_blocked`. Retain protected bindings; never manually delete
  blocked markers.
- Release only a health-classified `protected` binding. From one maintenance
  snapshot, calculate the marker and target SHA-256 values, then run:

  ```bash
  python "<PAO_SKILL>/scripts/oa.py" audit-preserve-release \
    --run-id <RUN_ID> \
    --segment events.<N>.jsonl \
    --expected-marker-sha256 <MARKER_SHA256> \
    --expected-segment-sha256 <SEGMENT_SHA256> \
    --decision release-protection \
    --root <BUS_ROOT>
  ```

  The command requires the exact valid marker/target/two-key binding and no
  duplicate target claim. It commits a deterministic
  `audit_preservation_released` event before revalidating and unlinking only
  the marker. If audit append fails, the marker remains. Retry the exact
  command after interruption; event-first and already-unlinked states
  converge. The target is never deleted or changed by release and becomes
  eligible for a later normal `prune`.
- `audit-health` exposes committed release history as
  `preservation_releases` with completed/resumable/blocked aggregate counts.
  One strict event and an absent marker is completed. An exact still-protected
  marker is an event-first resumable transaction; retry
  `audit-preserve-release` using its reported run, segment, marker, and target
  fingerprints. Duplicate events, deterministic-key/payload disagreement,
  marker fingerprint drift, or a blocked marker binding are reason-coded
  blocked. Never rewrite events or remove a blocked marker manually.
- Audit append failures degrade observability only; they are reported on stderr and never cancel an already-valid control/task/result operation. The active audit log rotates at a bounded segment size.
