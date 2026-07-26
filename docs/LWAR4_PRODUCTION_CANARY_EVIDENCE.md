# LWAR4 Production Canary Evidence

## Objective

Execute one preregistered `constraint_ordering` production canary after the
blind trial qualified LWAR4, then verify the live routing receipt, objective
outcome, and sticky circuit fallback without resetting negative evidence.

## Preregistration

Commit `332215976b25e8c05c51514b1161b563027eab0f` sealed the prompt hash,
answer hash, profile, policy, prior observation set, adapter contract, and
expected `LWAR4` live route before provider execution.

The task was read-only and network-disabled. Its answer key was represented
only by a SHA-256 in the tracked preregistration.

## Production result

The routing receipt matched the preregistration:

- selected alias: `LWAR4`
- route mode: `live`
- reason: `confidence_qualified_live`
- receipt SHA-256:
  `6b54eec1af715b76fd3935943c01eed56b1553a06d8d8895152563a0a59538f3`

The provider adapter made two verifier-accounted attempts and reported 23,377
tokens. The final JSON remained invalid for the required ordering alphabet
because its answer string contained internal whitespace. The deterministic
verifier returned `invalid_ordering_alphabet`; the LWAR submitted `failed` and
OA recorded `rejected`.

## Sticky fallback

The rejection opened `constraint_ordering::LWAR4` with reason
`candidate_rejected`. No circuit reset was performed.

A same-class fallback probe then recorded:

- candidate alias: `LWAR4`
- selected alias: `LWAR1`
- route mode and reason: `circuit_open`
- receipt SHA-256:
  `8b641a75c6ac5a319cbb1de08d3df4fff6b061382506a7147e044ebf3303d491`

LWAR1 returned the exact objective answer, OA accepted it, and the circuit
remained open. Audit ordering records `routing_circuit_opened` before the
fallback `routing_decided` event.

## Final state

- workflow: 2/2 terminal
- online observations: 96
- LWAR4 constraint ordering: 12/13 accepted, Wilson lower `0.666855`
- LWAR1 constraint ordering: 13/13 accepted, Wilson lower `0.771898`
- analyzer: `current_evidence_remains_shadow_only`
- promoted classes: none
- audit: healthy
- active claims and leases: zero
- shutdowns consumed: 4/4

Verdict:

`production_canary_rejected_sticky_fallback_verified`

Portable evidence is stored in
`benchmarks/lwar4-production-canary-evidence-v1.json`.

## Subsequent adapter recovery

The answer-key-free ordering correction and a fresh 12-task adapter shadow are
documented in `docs/LWAR4_ORDERING_RECOVERY_EVIDENCE.md`. The shadow accepted
12/12 answers, but all were accepted on the first provider call, so its live
corrective-feedback effect remains unproven.

The isolated run did not create PAO routing observations. The production
circuit remains open with zero resets; this negative production evidence is
unchanged.
