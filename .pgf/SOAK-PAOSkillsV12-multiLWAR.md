# Multi-LWAR Operational Validation — round 3

> Date: 2026-07-18 | Commit under test: `d7af275` (0.6.0) | Verdict: **PASS**
> First n>1 validation: everything before this round (soaks 1–2) was single-LWAR.
> Topology: OA = main session (`PAO_OA_ID=oa-main-soak`); two independent background
> agent sessions over the installed standalone skills against the shared bus.

## Nodes

- **LWAR1** — resumed from `off` (round-2 identity, generation 1), capabilities `coding, testing`
- **LWAR2** — fresh registration (0.6.0 handshake), capabilities `analysis, writing`
- Both `on` concurrently; per-mailbox isolation held throughout (0 quarantines all round).

## Phase 1 — capability auto-routing (5 probes)

| Probe | Requirement | Routed to | Correct |
|---|---|---|---|
| task-r3-c1 | coding | LWAR1 | ✓ |
| task-r3-c2 | coding+testing | LWAR1 | ✓ |
| task-r3-a1 | analysis | LWAR2 | ✓ |
| task-r3-a2 | writing | LWAR2 | ✓ |
| task-r3-gpu | gpu | explicit error "no eligible LWAR" | ✓ (no arbitrary fallback) |

All four routed tasks executed concurrently across the two nodes and succeeded
with byte-exact artifacts.

## Phase 2 — cross-LWAR `depends_on` DAG (workflow-r3-dag)

1. D2 (`depends_on: [task-r3-d1]`) published **before** D1 existed → rejected:
   `dependency not satisfied: no ledger entry` (gate closed).
2. D1 → LWAR1: wrote `word.txt = 'quanta'` in the shared workspace → collected.
3. D2 re-published → gate open → routed to LWAR2, which **read LWAR1's output**
   and produced `echo.txt = 'quantaquanta'` (byte-verified by OA).
4. `workflow-status`: 2/2 completed with the dependency edge recorded.

## Phase 3 — cancel drill

- LWAR1 occupied by task-r3-long (honest 5s-poll wait loop for a release file).
- task-r3-cxl queued behind it (priority 9), then `control cancel` sent.
- Outcome: LWAR1 claimed the control before the queued task, later claimed the
  task and submitted **`cancelled` without executing it** (`never.txt` was never
  created); the long task completed `succeeded` after the release file appeared.

## Ledger balance (audit, cumulative across rounds 1–3)

published 20 = received 20 = submitted 20; 3 identities adopted over 2 slots;
0 quarantines; every lifecycle transition requested by an LWAR and approved by
OA reconcile.

## Conclusions

- Orchestration at n>1 works end-to-end on 0.6.0: routing, isolation, DAG
  gating with real cross-node data flow, and queued-task cancellation.
- The cancel semantics that work in practice: control claims take priority over
  task claims at each slice, so a cancel for a queued task reaches the node
  first and the task is claimed later only to be terminally cancelled.
- Remaining residual (unchanged): agent-context compaction was again not
  triggered; to be observed in production use.
