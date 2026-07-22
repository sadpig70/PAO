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
- `shutdown` retains a resumable slot; `retire` completes the lifecycle through
  deregistration and returns the slot.

For execution, read the applicable role skill. No separate ADP bootstrap prompt
is valid.
