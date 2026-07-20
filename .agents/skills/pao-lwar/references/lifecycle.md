# LWAR Reference — Lifecycle and Status

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Status

For **your own** LWAR self-inspection, use the LWAR status command — it reads
your registry slot, verifies your `(lwar_id, instance_id, generation)` tuple, and
refreshes the local identity file's `state`/`registry_version`:

```bash
python "<PAO_SKILL>/scripts/lwar.py" status --identity-file IDENTITY_FILE
```

Exit codes: `0` = `lwar_status` (payload has `state`, `generation`, heartbeat);
`2` = registry unavailable; `3` = unregistered; `4` = identity mismatch.

These map to distinct bootstrap branches (§0.5) — do not collapse them to a
binary present/absent:

| Exit | Meaning | Bootstrap action |
|---:|---|---|
| `0` | slot present, identity matches | RESUME (never re-register) |
| `2` | registry momentarily unavailable | **transient** — wait briefly and re-run `status`; do **not** register (registering here would orphan a still-valid identity) |
| `3` | your slot is genuinely not in the registry | REGISTER (this is the only true register branch) |
| `4` | the slot exists but your identity tuple is stale (slot reused / generation bumped) | your old identity is dead — register **fresh** from a clean session; do not reuse the stale identity file |

(`oa.py status` also exists but is the OA's roster view of **all** LWARs; prefer
`lwar.py status` when inspecting yourself so your identity file stays current.)

## Lifecycle transitions

```bash
python "<PAO_SKILL>/scripts/lwar.py" state draining --identity-file IDENTITY_FILE
python "<PAO_SKILL>/scripts/lwar.py" state off --identity-file IDENTITY_FILE
python "<PAO_SKILL>/scripts/lwar.py" state on --identity-file IDENTITY_FILE
python "<PAO_SKILL>/scripts/lwar.py" state deregistered --identity-file IDENTITY_FILE
```

## Rules

- Transitions follow `on → draining → off → deregistered`. Request `deregistered` only from `off`.
- Lifecycle commands are **requests**: the registry state changes only after OA runs `reconcile` and approves. Do not assume a state is final until a status inspection confirms it.
- `draining`: finish current work, accept no new tasks. Who ends it depends on who started it: after an **OA-initiated** `control:drain`, keep watching until `shutdown` (OA owns the stop); request `off` yourself only in the **self-initiated** context-exhaustion handoff below.
- Deregistration frees the numeric slot; a future reuse of the slot bumps `generation`, and your old identity becomes permanently stale.

## Context-exhaustion handoff

Session context is finite; running out mid-claim would violate the terminal-result rule. Trigger this handoff only on an **objective** exhaustion signal — an explicit runtime context/token warning, or a measured token budget crossing a high threshold (e.g. ~90% of the window). Elapsed wall-clock time, many idle slices, or a subjective sense of "enough has happened" are **not** exhaustion and must not trigger it (§1.3) — that is the daemon-quitting-because-it-feels-finished bug. When a genuine exhaustion signal fires, hand off instead of dying:

1. Request `draining` (`state draining`) so no new task is claimed.
2. If a task is claimed, finish or stop it and submit its terminal result (`failed` or `blocked` with the reason is acceptable — never abandon it silently).
3. Request `off`. Either resume later in a fresh session by re-registering (a reused slot bumps `generation`, so every stale message from this session is quarantined automatically), or request `deregistered` to free the slot.

This handoff is the only sanctioned way to end ADP without an OA `shutdown`.
