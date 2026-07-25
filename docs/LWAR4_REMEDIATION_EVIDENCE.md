# LWAR4 Blind Remediation Evidence

## Objective

Classify the five prior LWAR4 failures, repair reusable adapter and harness
boundaries without exposing answer keys, and reevaluate canary promotion on a
preregistered non-overlapping suite.

## Failure classification

The five failed records represented three causal families:

- four valid-JSON objective failures caused by missing finite-task
  verification;
- one 180-second provider timeout that escaped the retry coordinator;
- benchmark diversity inflation: four duplicate optimization prompt pairs
  existed in the prior suite, including the failed `BO04/BO10` pair.

The remediation adds:

- a provider-neutral private verification preamble and supported `high`
  reasoning variant for OpenCode;
- an answer-key-free deterministic verifier for finite optimization and
  ordering responses;
- one corrective provider call when deterministic checks fail, with all call
  tokens retained;
- typed `timeout_180s` results so the existing bounded retry policy applies;
- a suite generator that rejects duplicate prompts and overlap with the prior
  suite.

Unknown timeout telemetry remains excluded rather than estimated.

## Preregistration

The final blind suite contains 12 unique bounded-optimization tasks and 12
unique constraint-ordering tasks. Prompt overlap with the prior suite is zero.

- suite and initial adapter commit: `e26adac`
- final adapter contract commit: `770ac5a`
- suite SHA-256:
  `a32bda87a352001259647c00302afb3696438c37e8544d0d1ff8d0793ba24410`
- answer-key SHA-256:
  `ab003729818d06ef632de92b1c848e19c371710df6482ca8da355d53b9312e8e`
- adapter-contract SHA-256:
  `1c788666dd70081aefcceaa3c83d868c692449cdb6ce2369ed1ec31ecd05bf41`

The known-failure diagnostic passed 5/5 after the final remediation. It is
explicitly excluded from blind evidence.

## Blind result

The fresh four-alias run executed 96 explicit side-effect-free shadow tasks.

- provider calls completed: 92/96
- objective acceptance: 92/96
- bound online observations: 94
- missing-token exclusions: 2
- audit: `healthy`
- shutdown consumed: 4/4
- circuit resets: 0

| Task class | LWAR1 | LWAR2 | LWAR3 | LWAR4 | Decision |
|---|---:|---:|---:|---:|---|
| bounded optimization | 12/12 | 12/12 | 12/12 | 8/11 | keep LWAR1 leader |
| constraint ordering | 12/12 | 12/12 | 11/11 | 12/12 | LWAR4 `confidence_qualified_live` |

For constraint ordering, LWAR4 and LWAR1 both have a 95% Wilson lower bound of
`0.757499`, while LWAR4 mean reported tokens are `11,485` versus LWAR1
`18,879`, about 39% lower.

For bounded optimization, LWAR4 remains blocked: its Wilson lower bound is
`0.434350` versus LWAR1 `0.757499`, and verifier-triggered retries raise its
mean reported tokens above the incumbent.

Verdict:

`constraint_ordering_promoted_bounded_optimization_blocked`

## Superseding production status

The blind qualification above is historical evidence, not the current live
state. A subsequent preregistered production canary routed live to LWAR4 and
was objectively rejected. The rejection opened the sticky
`constraint_ordering::LWAR4` circuit, and a same-class probe routed to LWAR1
with `route_mode=circuit_open`.

The current analyzer verdict is `current_evidence_remains_shadow_only`; no
class remains promoted. See `docs/LWAR4_PRODUCTION_CANARY_EVIDENCE.md`.

Portable aggregate evidence is stored in
`benchmarks/lwar4-remediation-evidence-v1.json`.

## Reproduction

```bash
python tools/run_lwar4_remediation_diagnostic.py \
  --suite benchmarks/canary-online-suite-v1.json \
  --output path/to/diagnostic

python tools/run_heterogeneous_lwar_ab.py \
  --root path/to/isolated-bus \
  --task-suite benchmarks/lwar4-remediation-suite-v1.json \
  --canary-profile path/to/calibration-profile.json \
  --canary-policy benchmarks/canary-policy-v1.json

python tools/run_canary_router_evidence.py \
  --profile path/to/calibration-profile.json \
  --policy benchmarks/canary-policy-v1.json \
  --bus-root path/to/isolated-bus \
  --output path/to/report
```

The suite must not be regenerated after preregistration when reproducing the
recorded evidence. `python tools/build_lwar4_remediation_suite.py` is only for
deterministic source verification before a new preregistration.
