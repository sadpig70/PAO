# SOAK Round 2 — PAOSkillsV11 (long-horizon endurance)

> Date: 2026-07-17 | Commit under test: `943331e` | Duration: 2h05m (06:44–08:50 UTC)
> Verdict: **PASS** — loop survival, contract adherence, and the exhaustion-handoff
> procedure proven in real operation. One residual: context compaction never
> triggered, so post-compaction adherence remains unexercised.

## Setup

Same two-session topology as round 1 (installed skills, central bus). The LWAR
**resumed** its round-1 identity (LWAR1, generation 1) from the `draining` slot via a
`state on` lifecycle request — the resume path works; generation correctly unchanged
(no slot reuse). OA side ran a detached publisher: 5 bursts separated by ~25-minute
idle gaps, each cycle doing send → settle → recover → collect → status snapshot.

## Results

- **81 watcher slices** (9 task_received + 72 idle_timeout), zero `adp_error`,
  zero `state_wait`, safety bounds (200 slices / 3-repeat errors) never approached.
- **9/9 tasks correct**: 8 succeeded with byte-exact artifacts (verified `od -c`
  agent-side and re-verified on disk by OA), 1 deliberately-impossible task submitted
  as an honest terminal `failed` with no fabricated output.
- **0 quarantines, attempt=1 throughout** (no lease expiry: lease alignment
  `timeout_s+30` held through every execution).
- Heartbeat fresh (<5s) at every publisher checkpoint across all idle gaps.
- Audit ledger balanced across both rounds: 12 published = 12 received = 12 submitted;
  lifecycle: draining(r1) → on(r2 resume) → draining → off (all requested by LWAR,
  approved by OA reconcile, each on the first status poll).
- **Handoff drill passed**: `state draining` → terminal-result check → `state off`,
  slot retained at generation 1 — the v1.1 context-exhaustion procedure executes
  cleanly end-to-end.

## Friction found → disposition

| # | Finding | Disposition |
|---|---|---|
| 1 | `complete --result-file` relative path resolves against process CWD, not bus root; the doc example only worked when cwd == bus root | **Fixed**: execute-complete.md now shows an absolute-path example and states the resolution rule |
| 2 | `--state-wait-backoff-max` never exercised (state stayed `on`; wind-downs reconciled on first poll) | No action — noted as untested-in-anger |
| 3 | Hand-typing the long absolute invocation 80+ times produced one caught typo (exit 2, no protocol impact) | Ergonomics note: PAO_ROOT env is the designed answer; harnesses that reset env per call should shell-alias the invocation |

## Operational insights

- **Chat messages do not reach a busy LWAR** — a mid-loop agent never ends its turn,
  so harness messaging is delivery-blocked until idle. The bus task channel worked
  immediately (the handoff drill was delivered as a priority-1 task). This validates
  PAO's own premise: the bus, not the harness, is the OA→LWAR channel.
- Both rounds needed one OA nudge to (re)start the watch loop after registration/resume
  — LWAR-side harnesses tend to end their turn at natural pauses. Candidate SKILL
  hardening for v1.2: an explicit "starting ADP means entering the loop in the same
  turn" sentence in Absolute Rules.

## Residual for the gate

Compaction never occurred in 2h05m (the agent's transcript stayed within budget —
72 of 81 slices were silent idles, which is exactly what the no-narrative rule is
for). Post-compaction contract adherence therefore remains unproven. Judgment:
loop survival + honest terminals + handoff are the load-bearing claims and all
passed in real operation; compaction resilience is documented as a known residual
to be observed in production use rather than gating the backport.

**Gate recommendation: OPEN the plugin backport, residual documented.**
