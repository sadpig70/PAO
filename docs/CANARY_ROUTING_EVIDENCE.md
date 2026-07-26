# Confidence Canary Routing Evidence

## Safety objective

No non-incumbent LWAR may receive production traffic until every
profile-known eligible alias has at least ten accepted observations in the
task class and the candidate's 95 percent Wilson lower confidence bound is
non-inferior to the calibration incumbent.

## Contract

- The v1 calibration profile remains immutable and selects the incumbent and
  token candidate.
- The canary policy schema enforces a minimum accepted floor of ten.
- Before promotion, production executes on the incumbent and records the
  candidate in a strict canary receipt.
- `--routing-shadow` is explicit and requires `permissions.write=[]` plus
  `permissions.network=false`.
- `--routing-shadow-lwar-id` collects the same read-only evidence for any
  explicitly eligible alias without changing the production candidate.
- Online evidence comes only from recorded OA semantic validation and is bound
  to its receipt and task-ledger validation SHA-256.
- A rejected live candidate or confidence-detected window decline opens a
  sticky alias/class circuit. Only a reason-bound, identity-bound OA reset can
  close it.

## Current online evidence

A fresh four-provider, current-generation run executed 30 objective tasks per
alias: ten constraint-ordering, ten code-review, and ten bounded-optimization
tasks. All 120 tasks were explicit side-effect-free shadows. Provider execution
completed for 119/120 calls, objective validation accepted 113/120, and 117
records had complete token telemetry and became bound online observations.
Three incomplete telemetry records were excluded rather than estimated.

| Task class | Incumbent | Candidate | Accepted observations LWAR1/2/3/4 | Outcome |
|---|---|---|---|---|
| bounded optimization | `LWAR1` | `LWAR4` | `10/10, 9/10, 10/10, 6/9` | incumbent only |
| code review | `LWAR1` | none | `10/10, 9/10, 8/8, 10/10` | incumbent only |
| constraint ordering | `LWAR1` | `LWAR4` | `10/10, 10/10, 10/10, 9/10` | incumbent only |

Verdict:

`current_evidence_remains_shadow_only`

Only current-generation online observations counted toward the floor;
calibration observations selected the incumbent/candidate but did not count
toward promotion. The lower-token `LWAR4` candidate failed both eligible
classes' balanced accepted floor and Wilson non-inferiority check. Production
therefore remained on `LWAR1`. Audit health was `healthy`, all four shutdown
commands were consumed, and no circuit reset was used.

The verified evidence hashes are:

- profile: `cd1f42fb515eaa2472654351b57b5110f3dbf9cd03cdac3d8026750733f1af29`
- policy: `503f1c1db5b06446a24fe4f594f3df40d3f17d8ca8c7accf28ba5d6546d713e6`
- portable aggregate: `benchmarks/canary-online-evidence-v1.json`

## Reproduction

```bash
python tools/build_canary_online_suite.py

python tools/run_heterogeneous_lwar_ab.py \
  --root path/to/isolated-bus \
  --task-suite benchmarks/canary-online-suite-v1.json \
  --canary-profile path/to/calibration-profile.json \
  --canary-policy benchmarks/canary-policy-v1.json

python tools/run_canary_router_evidence.py \
  --profile path/to/calibration-profile.json \
  --policy benchmarks/canary-policy-v1.json \
  --bus-root path/to/isolated-bus \
  --output path/to/report-directory
```

The tool emits canonical profile, policy, and observation-set hashes plus the
per-class route decision. `--experiment` remains available for preregistered
replay evidence. Neither mode infers missing telemetry.

The subsequent non-overlapping LWAR4 remediation trial is documented in
`docs/LWAR4_REMEDIATION_EVIDENCE.md`. It qualifies LWAR4 for live
constraint-ordering routes while retaining LWAR1 for bounded optimization.

That qualification was exercised by the production canary documented in
`docs/LWAR4_PRODUCTION_CANARY_EVIDENCE.md`. The live result was rejected,
opened the LWAR4 constraint-ordering circuit, and successfully forced the next
same-class route to LWAR1. The current state has no promoted class.

The subsequent adapter-only ordering recovery is documented in
`docs/LWAR4_ORDERING_RECOVERY_EVIDENCE.md`. Its fresh suite passed 12/12, but
the correction path was not triggered live and the run is not current-
generation PAO routing evidence. The LWAR4 constraint-ordering circuit
therefore remains open.
