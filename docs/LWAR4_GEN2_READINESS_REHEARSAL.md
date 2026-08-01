# LWAR4 generation-2 readiness rehearsal

## Purpose

This note records a provider-free rehearsal of the mechanical path that a
replacement LWAR4 provider session traverses, and ties it to the already-spent
generation-2 calibration campaign. It exists so a future replacement attempt has
a verified command sequence and a clear record of which gate actually stopped
generation 2.

The rehearsal ran entirely on an isolated throwaway bus outside the repository
(on a local filesystem chosen to avoid the Defender atomic-replace race), used
clearly labelled synthetic profiles,
never impersonated a real provider, and never touched the production bus
(`.pao/`) or the preserved `constraint_ordering::LWAR4` circuit. The throwaway
bus was deleted afterward.

## Verified mechanics (2026-08-01)

Each step below was executed against the real runtime CLIs and its invariant
confirmed.

1. **Register + generation increment** — register the exact `LWAR4` slot for a
   first (synthetic z_ai) profile, reconcile to generation 1, adopt, then take
   it `on -> off -> deregistered`. Deregistration writes a tombstone with
   `last_generation = 1`. Re-registering the exact `LWAR4` slot then yields
   **generation 2** (`registry.py`: `generation = tombstone.last_generation + 1`),
   with a new `instance_id` and the new profile. A stale-generation actor cannot
   reclaim generation 1 (`identity_mismatch`), and premature reuse inside the
   tombstone retention window is refused (`lwar_id_tombstoned`).

   ```
   lwar register 4 --runtime-name <name> --model <model> \
       --adapter-id <id> --vendor-family <family> --interface cli --root <BUS>
   oa reconcile --root <BUS>                 # generation N assigned
   oa reconcile --root <BUS> --tombstone-retention 0   # only if replacing a just-retired slot
   ```

2. **Atomic adopt** — `lwar response <request_id> --root <BUS>` materialises the
   identity file with the correct generation. The real replacement uses
   `response --resident` to adopt and enter the resident ADP loop in the same
   process. The rehearsal stops before the resident loop — that is where a live
   provider session lives.

3. **Binding gate (fail-closed)** — with no bound provider, the host adapter
   refuses execution:
   - `host_adapter qwen-probe --command <cmd>` reports `eligible: false`
     (`reason_codes: ["command_not_found"]`);
   - `host_adapter qwen-run ...` with a valid `host_contract` but no reachable
     provider writes a `status: "rejected"` execution receipt and runs nothing.

   This is the machine enforcement of
   `identity_and_adapter_binding=pending_before_provider_execution`: no
   calibration task can execute until a real identity and adapter are bound.

Conclusion: the register -> generation increment -> adopt -> binding-gate path is
mechanically sound. A future replacement will not lose a one-shot campaign to a
registration or binding defect.

## Why generation 2 already stopped (not a mechanics failure)

The mechanics above are *not* what stopped generation 2. A real Qwen provider
session did register as generation 2 and executed the first recovery task on
2026-07-27 (`benchmarks/lwar4-generation2-calibration-evidence-v1.json`):

- RR01 objective answer was **correct** (`EADBCF`), but the provider reported
  solving it with a prohibited tool (`python rr01_solve.py`) and could not
  report exact token telemetry;
- the preregistered recovery gate therefore **failed closed** (`passed: false`),
  OA recorded a semantic rejection, and RR02-RR12 / RQ01-RQ13 / reset /
  production canary / fallback were **not executed**;
- the `constraint_ordering::LWAR4` circuit stayed **open** (before/after SHA
  identical, no routing observation recorded);
- the single authorised campaign execution is now **consumed**.

Verdict on record:
`generation2_recovery_failed_tool_contract_and_telemetry_gate_circuit_preserved_open`.
Provider-family heterogeneity was observed; exact-model heterogeneity and any
token-efficiency claim remain unproven.

## What a successful next attempt requires

1. A replacement provider that solves the finite ordering tasks **natively**,
   with no prohibited tool use, **and** reports **exact** runtime token counts
   (Qwen reported neither).
2. A **fresh generation** (generation 3 via another retire -> re-register cycle)
   because the generation-2 campaign is spent.
3. A decision on calibration material: the sealed generation-2 suite
   (`benchmarks/lwar4-generation2-calibration-suite-v1.json`,
   SHA `b253145e…`) was bound to one campaign execution — whether generation 3
   reuses it or requires a freshly sealed, nonoverlapping suite is an open
   design decision, not yet made.

Until such a provider session exists, the honest terminal state stands: circuit
open, no reset, no production qualification.

## References

- `docs/LWAR4_GENERATION_REPLACEMENT.md` — state, sealed material, campaign result
- `benchmarks/lwar4-generation2-calibration-evidence-v1.json` — terminal evidence
- `benchmarks/lwar4-generation2-calibration-preregistration-v1.json` — identity/adapter binding
- `.pgf/DESIGN-StaleLWARReplacement.md`, `.pgf/WORKPLAN-StaleLWARReplacement.md`
