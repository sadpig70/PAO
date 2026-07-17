# OA Reference — Registration and Lifecycle Reconciliation

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Commands

```bash
python "<PAO_SKILL>/scripts/oa.py" reconcile
python "<PAO_SKILL>/scripts/oa.py" status
python "<PAO_SKILL>/scripts/oa.py" status --stale-after 120
```

## Rules

- `reconcile` processes registration and lifecycle requests against schema and identity rules, then atomically assigns the lowest available `LWARn`. Slots in `on`, `draining`, or `off` remain occupied.
- A registration stamped with a `runtime_version` different from this bundle's version is rejected fail-closed (`runtime_version_mismatch`). A request without the stamp is a pre-0.5 legacy request and is accepted during the current freeze window.
- After approval, the LWAR itself fetches the response and adopts the identity; OA never writes identity files on the LWAR's behalf.
- Lifecycle transitions follow `on → draining → off → deregistered`; approve `deregistered` only from `off`.
- When a numeric slot is reused, the registry bumps `generation`; messages carrying an old `generation` or `instance_id` are stale and must never be treated as current.
- `status` computes heartbeat staleness (`heartbeat_stale`, default threshold 120s via `--stale-after`). A stale heartbeat is a recovery signal, not proof of death — see [recover-maintain.md](recover-maintain.md).

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
