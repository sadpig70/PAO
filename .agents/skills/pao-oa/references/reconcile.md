# OA Reference — Registration and Lifecycle Reconciliation

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Commands

```bash
python "<PAO_SKILL>/scripts/oa.py" presence
python "<PAO_SKILL>/scripts/oa.py" reconcile
python "<PAO_SKILL>/scripts/oa.py" status
python "<PAO_SKILL>/scripts/oa.py" status --stale-after 120
python "<PAO_SKILL>/scripts/oa.py" status --startup-deadline 30
```

## Rules

- `presence` publishes a 90-second OA liveness record. Use a 25-second refresh
  target and a 30-second hard latest while this session is actively supervising.
  Every mutating OA command refreshes it automatically; the explicit command
  covers idle periods.
- LWARs read `presence.json` through `lwar.py oa-status` and classify OA as
  `live`, `stale`, `missing`, or `invalid`. Never use the 900-second writer
  lease as proof that an OA process is alive.
- `reconcile` processes registration and lifecycle requests against schema and identity rules, then atomically assigns the lowest available `LWARn`. Slots in `on`, `draining`, or `off` remain occupied.
- A registration stamped with a `runtime_version` different from this bundle's version is rejected fail-closed (`runtime_version_mismatch`). A request without the stamp is a pre-0.5 legacy request and is accepted during the current freeze window.
- After approval, the LWAR itself fetches the response and adopts the identity; OA never writes identity files on the LWAR's behalf.
- Lifecycle transitions follow `on → draining → off → deregistered`; approve `deregistered` only from `off`.
- When a numeric slot is reused, the registry bumps `generation`; messages carrying an old `generation` or `instance_id` are stale and must never be treated as current.
- Identity adoption writes a matching `starting` heartbeat. Normal LWAR startup
  uses atomic `response --resident`, so the same Python process must replace it
  with the first resident watcher heartbeat within 30 seconds (configurable for
  observation with `status --startup-deadline`). A deadline miss is therefore
  an in-process startup failure, not ordinary agent scheduling latency.
- `status` reports `runtime_status=registered_not_started` when there is no
  current-identity heartbeat or the startup deadline was missed,
  `runtime_status=starting` inside the startup window, `active` for a fresh
  watcher heartbeat, and `stale` only for a watcher that had started and later
  exceeded `--stale-after` (default 120s). `heartbeat_identity_match` fences an
  old generation from the current slot.
- Missing, corrupt, identity-mismatched, `starting`, or stale heartbeats are
  excluded from `send --auto`; use explicit `--lwar-id` only when the operator
  intentionally overrides routing health.
- Registry, request, response, identity, heartbeat, lease, task, control, result, ledger, and validation payloads are checked against the bundled schemas at their trust boundaries.

## Autonomous supervision cadence

Unlike the LWAR's ADP watch loop, OA has no blocking resident loop — it acts on
demand. The skill's default `start` action nevertheless owns the supervisory
role: while registrations, lifecycle requests, or workflows are active, cycle
these on a cadence (event-driven when you can observe the bus, otherwise a light
poll — there is no busy-wait requirement):

```text
presence    → announce that this OA session is actively supervising
reconcile   → approve any new registration/lifecycle requests
status      → check runtime_status + startup deadline + heartbeat freshness
collect     → gather any submitted results (then validate)
recover     → requeue/dead-letter expired-lease claims; reconcile failed/ entries
```

Use deadline-aware scheduling: after each successful presence-publishing command,
set `next_presence_deadline = monotonic_success_time + 25s`. Recompute the
remaining budget after every foreground step, refresh immediately when due, and
cap every idle wait to the remaining time. Do not sleep 30 seconds after
completing a cycle, because command and tool overhead would then violate the
30-second hard latest. The 90-second presence TTL retains room for transient scheduling delays.
A deliberate OA stop needs no cleanup:
presence naturally becomes `stale`, while the writer lease continues to fence
mutations until its own expiry.

Between cycles the OA session may idle or serve the user; nothing is lost because
all state lives on the bus. If no user goal exists, supervise existing work but
never invent tasks. Escalate only when there is no eligible LWAR for an actual
goal, a result is genuinely undecidable, or dead-letters need a decision. Do not
ask for a second bootstrap prompt after a no-action `/pao-oa` invocation.

## State transitions

| Transition | Requested by | Approved by | Precondition |
|---|---|---|---|
| registration → `on` | LWAR `register` | OA `reconcile` | slot free, no tombstone block, runtime version compatible |
| `on` → `draining` | LWAR `state` or OA `control drain` | OA `reconcile` | — (no new publishes from this point) |
| `draining` → `on` / `off` | LWAR `state` | OA `reconcile` | `off` only when idle |
| `off` → `on` | LWAR `state` | OA `reconcile` | identity tuple matches |
| `off` → `deregistered` | LWAR `state` | OA `reconcile` | frees the slot; tombstone window guards reuse; next occupant gets `generation+1` |
| `incoming` → `claimed` | LWAR watcher | bus atomic move | identity tuple matches, state `on` |
| `claimed` → terminal result | LWAR `complete` | OA `collect` + validation | generation and attempt match the ledger |
| expired claim → requeue / dead | OA `recover` | retry budget | `attempt > max_retries` dead-letters |
