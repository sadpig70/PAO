# SOAK Round 1 — PAOSkillsV11 (bounded operational soak)

> Date: 2026-07-17 | Commit under test: `93d3d5c` | Verdict: **PASS (round 1 scope)**
> Setup: skills installed by folder copy to `~/.claude/skills/{pao-oa,pao-lwar}`;
> central bus `D:/PAO/_workspace/pao_bus`; OA = main session (`PAO_OA_ID=oa-main-soak`),
> LWAR = independent background agent session following the installed SKILL contract.

## Scenario executed

1. `pao doctor` pre-flight on both installed bundles — healthy (9/9 checks each).
2. LWAR self-registered with real metadata → OA `reconcile` approved → `LWAR1` gen 1
   (runtime_version handshake 0.5.0 = 0.5.0 passed implicitly).
3. Three tasks published (one via `send --auto --require-capability coding`):
   - task-soak-1: byte-exact file write → **succeeded**, artifact verified on disk by OA
   - task-soak-2: computed sum → **succeeded**, artifact verified (5050)
   - task-soak-3: required input deliberately missing → **honest terminal `failed`**,
     nothing fabricated
4. `control:ping` mid-loop → watcher re-run, no narrative pollution.
5. `collect --archive`: 3 collected, 0 quarantined; results carried `attempt=1` and a
   valid `claim_token`. `validate`: 2× ready_for_oa_review, 1× attention_required — all correct.
6. `control:drain` → LWAR read lifecycle.md, requested `draining`, OA reconciled
   (registry_version→2), watcher continued in state_wait with `--state-wait-backoff-max 30`.
7. `control:shutdown` → ADP terminated cleanly. 6 watch slices total, 0 anomalies,
   well inside the 40-slice safety bound.

## Contract friction found (LWAR agent feedback) — all fixed same day

| # | Friction | Fix applied |
|---|---|---|
| 1 | `response` exit codes (0/2/3) undocumented in register.md | Exit-code table added to register.md |
| 2 | "exactly" criteria ambiguous about trailing newline | Byte-exact default in execute-complete.md + authoring note in publish.md |
| 3 | drain contradiction: adp-loop "keep watching" vs lifecycle "request off when idle" | lifecycle.md now splits by initiator: OA-initiated drain → keep watching for shutdown; self-initiated exhaustion handoff → request off |
| 4 | adp-loop drain row lacked lifecycle.md cross-reference | Cross-reference added |

Contract tests + full suite re-run after fixes: 66/66 OK. Installed copies re-synced.

## Operational insights

- After `shutdown`, the slot remains `draining` — by design the LWAR does not
  self-transition to `off` on an OA-owned stop. Resuming later reuses the persisted
  identity file, or the slot is cleaned up by a future lifecycle request. Bus left
  in place for inspection.
- Observed agent-side friction (not a bus defect): the LWAR's registration-response
  poll stalled in its own harness (~9 min gap between adoption and first slice) until
  nudged; the bus state remained consistent throughout — no message loss, no stale state.

## Gate status

- Round 1 (contract adherence, real two-session operation): **PASS**.
- Still unproven — the original soak concern: **long-horizon survival** (hours of
  slices, context compaction resilience, exhaustion handoff under real pressure).
  The plugin-backport gate stays closed until a long-horizon round passes.
