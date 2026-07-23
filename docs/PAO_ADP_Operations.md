# PAO ADP Operations Overview

This document describes the operating model without duplicating the executable
contract. The canonical instructions are:

- OA: `.agents/skills/pao-oa/SKILL.md` and its bundled references
- LWAR/ADP: `.agents/skills/pao-lwar/SKILL.md` and its bundled references

## Runtime Model

The user starts one OA session and one or more LWAR sessions on the same local
bus, in either order. Each session receives only the instruction to read its role
skill and act in that role. OA publishes a short-TTL presence signal while it
supervises durable workflows; each LWAR can distinguish live, stale, missing,
and invalid OA presence, self-registers, adopts an approved `LWARn` identity,
and remains in its ADP watch/execute loop.

```text
OA: presence -> reconcile -> plan/publish -> monitor -> collect/validate -> recover
LWAR: oa-status -> register/adopt -> watch -> execute/submit -> watch -> shutdown/retire
```

All deterministic bus operations, exit-code reactions, controls, result states,
claim fencing, lifecycle transitions, and recovery rules live in the two role
skills. Operators and runtimes must not reconstruct those mutable commands from
this overview.

## Deployment Boundary

- Both roles use the same bus selected by `--root`, `PAO_ROOT`, or `<cwd>/.pao`.
- The bus must be on a single-host local filesystem.
- Task execution occurs in each TaskContract's own `cwd` and authority bounds.
- OA never drives a vendor CLI directly; LWARs receive work only through PAO.
- All mutating OA commands are process-serialized, including commands sharing
  the same `PAO_OA_ID`; read-only status and validation remain lock-free.
- If an OA dies while holding the command mutex, a later command automatically
  reclaims the stale lock only after confirming the recorded PID is dead.
- Mailbox, registry, identity, lease, and ledger files are never edited by hand.

## Operator Expectations

- OA approval is required before a registering runtime may claim `LWARn`.
- OA and LWAR startup order is independent; an LWAR waits when OA presence is
  absent or stale and never self-approves.
- LWAR ADP remains resident across idle watch slices.
- Every claimed task produces exactly one terminal result.
- OA validates completion criteria and evidence; process exit alone is not
  success.
- Expired or superseded claims are recovered and fenced by the bundled runtime.
- A `starting` identity that misses its startup deadline remains non-routable;
  OA can reclaim it only through the exact-identity, empty-mailbox
  `recover --reap-startup` guard.
- Interrupted startup reaping is retried with the same identity tuple;
  tombstone-first ordering keeps partial commits fenced until retry convergence.
- When state committed but the response was lost, the same retry returns
  `already_reaped` without rewriting state and restores the audit trail.
- Startup-reap audit replay is key-idempotent: committed audit steps are not
  duplicated, while a missing later step is appended during recovery.
- Repeated active-audit failures retain one degraded entry per deterministic
  key under a dedicated spool lock. If promotion stops after active flush but
  before spool deletion, retry filters committed keys and removes the spool
  without duplicating the event.
- Active and degraded audit appends use `flush` plus `fsync`; an active `fsync`
  failure retains the event in the durable spool instead of deleting recovery
  evidence.
- Rotated audit pruning uses the append lock order and retains any segment whose
  deterministic key is pending in the degraded spool. Healthy replay clears
  the spool, allowing a later prune to enforce retention safely.
- Deterministic append/replay scans every active and rotated audit segment. If
  any segment is unreadable, append is deferred and the event remains degraded
  until a complete scan succeeds.
- Audit JSONL accepts valid object lines, including unkeyed events. A malformed
  non-terminated tail in mutable active/degraded storage is durably copied to
  `var/audit/.corrupt/` before bounded truncation. Any other malformed line is
  preserved in place and requires operator repair before keyed append resumes.
- Run `oa.py audit-health` for a read-only audit diagnosis. Exit `2` means
  blocked keyed append/replay; the JSON identifies malformed lines, repairable
  tails, pending degraded records, quarantines, and exact `repair_candidates`
  with segment SHA-256 and line numbers without creating locks, leases,
  presence, or audit events.
- For non-automatic cases, run `oa.py audit-repair --segment NAME
  --expected-sha256 HASH --drop-line N` and repeat `--drop-line` for every
  malformed line from the same diagnosis. The command rejects changed bytes or
  any selection that is not exactly the malformed set, preserves the original
  under `.corrupt/`, then atomically restores a valid segment and degraded
  replay. Never hand-edit the segment between diagnosis and repair.
- Audit repair persists a `.repairs/` receipt before replacement. Its
  `prepared`, `replaced`, and `committed` phases let the exact command converge
  after interruption on either side of target replacement or audit append.
  `audit-health` reports `repair_receipts` and `pending_repair_count`; receipt,
  backup, argument, or target drift is never inferred as success.
- `prune` removes old repair receipts and backups only when a `committed`
  receipt, deterministic audit key, matching original backup, and
  repaired-or-consumed target state all agree. All incomplete, invalid,
  missing, drifted, or ambiguous repair evidence is retained. Receipt and
  backup removal counts are reported separately.
- Eligible cleanup first writes a `.repair-prune/` tombstone. The runtime
  removes the receipt, atomically stages the matching backup, records
  `backup_staged`, removes the staged backup, and deletes the tombstone last.
  Run `prune` again after interruption; the authorized transaction resumes at
  every process-stop boundary even if the new cutoff differs. Any malformed or
  conflicting transaction remains untouched.
- `audit-health` lists `retention_tombstones` and classifies each as
  `resumable` or reason-coded `blocked` without acquiring locks or changing
  files. The aggregate counts raise health to `attention`, not audit mutation
  blockage. Run `prune` for resumable entries. Preserve blocked evidence and
  repair the condition named in `reason_codes`.
- Rotated pruning protects every retention tombstone's named rotated repair
  target and every rotated segment containing its deterministic repair audit
  key. This applies to both resumable and blocked transactions. If any
  tombstone is malformed, unreadable, or not a file, the entire rotated prune
  pass removes nothing. Protection ends when retention completion removes the
  tombstone.
- Before deleting an age-eligible rotated segment, `prune` validates the full
  file as JSON-object JSONL. Invalid or inaccessible files remain untouched.
  The output separates `audit_segments_removed`,
  `audit_segments_protected`, and `audit_segments_blocked`; only removed files
  contribute to `total`. `audit_segment_outcomes` supplies a bus-root-relative
  `path`, matching status, and stable `reason_codes` for every counted segment.
  Its length equals the three counts combined, and the `pruned` audit event
  preserves the same ordered list.
- Before deleting any `valid_expired` segment, `prune` durably writes one
  `.rotated-prune/<run_id>.json` receipt with its cutoff, decisions, and exact
  file fingerprints. Retry after interruption; the sole pending run resumes
  before new classification. Missing authorized targets are already complete,
  remaining targets must match their witnesses, and drift is retained as
  `segment_drifted`. Receipt removal is gated by confirmation of deterministic
  audit key `rotated-prune:<run_id>`. When
  `audit_prune_audit_committed=false`, repair audit health and rerun `prune`.
- `audit-health` reads `.rotated-prune/` without locks or mutation. It lists
  `rotated_prune_receipts` and resumable/blocked counts. Valid interruption
  topologies are `resumable`; invalid schema, multiple/unexpected entries,
  witness drift, unreadable/non-file targets, incomplete audit snapshots, and
  an `applied` target that is present are stable reason-coded `blocked`.
  Receipt health raises overall status to `attention` without changing
  `keyed_append_blocked`. Run `prune` for resumable entries; preserve and
  investigate blocked evidence.
- Resolve only `applied_target_present` with `audit-prune-resolve`. Supply the
  exact run ID and current receipt/segment SHA-256 values from the same
  maintenance snapshot, plus `--decision preserve-recreated`. The command
  writes a fingerprint-bound `.rotated-preserve/` marker before changing the
  receipt, preserves the segment, recovers the original deterministic `pruned`
  event, and records an exact-once resolution event. Both audit keys and the
  unchanged marker/target are required before receipt completion. Future
  pruning reports `operator_preserved_target`. Any identity, fingerprint,
  schema, or receipt-count mismatch is refused.
- `audit-health` snapshots `.rotated-preserve/` without locks or mutation. It
  reports each marker in `rotated_preservations` plus protected/blocked
  aggregate counts. Protection is valid only when the regular target matches
  the marker fingerprint and both the original prune key and resolution key
  exist in the complete audit snapshot. Orphaned markers, fingerprint drift,
  duplicate target claims, invalid entries, and missing audit keys are stable
  reason-coded blocked states. Both protected and blocked markers raise
  overall health to `attention` without changing `keyed_append_blocked`.
  Preserve blocked evidence for repair; do not delete it manually.
- Release only a `protected` entry with `audit-preserve-release`. Supply the
  exact run ID, segment, marker SHA-256, and segment SHA-256 from one
  maintenance snapshot, plus `--decision release-protection`. The command
  verifies the valid marker/target/two-key binding and rejects duplicate target
  claims. It commits one deterministic release event before revalidating and
  removing only the marker. Audit failure retains the marker; exact retry
  converges before or after marker unlink. The segment remains byte-identical
  and may be removed later by normal age-based `prune`.
- `audit-health` reports `preservation_releases` plus completed, resumable, and
  blocked counts. A valid committed release event with no marker is completed
  history and does not raise health. If its exact protected marker remains,
  the event-first transaction is resumable: retry `audit-preserve-release`
  using the reported fences. Duplicate events, key/payload conflict, marker
  fingerprint drift, and blocked marker bindings raise `attention` with stable
  reason codes but do not change `keyed_append_blocked`. Do not manually edit
  release events or delete blocked markers.
- `shutdown` retains a resumable slot; `retire` completes the lifecycle through
  deregistration and returns the slot.

For execution, read the applicable role skill. No separate ADP bootstrap prompt
is valid.
