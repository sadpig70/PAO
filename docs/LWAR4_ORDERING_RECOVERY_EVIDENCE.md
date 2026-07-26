# LWAR4 Ordering Adapter Recovery Evidence

## Objective

Repair the reusable ordering-output correction without exposing an answer key,
then execute one preregistered, non-overlapping adapter shadow while preserving
the open production circuit.

## Contract

Commit `c1a5067` sealed the 12-task suite, answer-key hash, adapter contract,
feedback source, maximum two internal verifier-accounted attempts, and
single-execution rule before provider execution.

The correction derives only a finite output alphabet and length from the
prompt. For `invalid_ordering_alphabet`, it requires the exact
`^[A-X]{N}$` form, each symbol once, and no whitespace, punctuation,
separators, or extra characters. The provider never receives the expected
answer.

All 12 prompts are distinct from the prior online and remediation suites and
from the rejected production-canary prompt.

## Adapter-shadow result

The sealed suite ran once in isolated per-task work directories:

- accepted: 12/12
- exact match: 12/12
- first-call accepted: 12/12
- internal correction triggered: 0/12
- complete token telemetry: 12/12
- reported tokens: 130,102

The adapter therefore passed the fresh ordering suite, but the real run did
not exercise the new corrective feedback. A deterministic mocked production
failure reproduces `invalid_ordering_alphabet`, verifies that the second
prompt contains format-only guidance and no expected answer, accepts the
corrected response, and retains both attempts' token accounting. This proves
the correction mechanism deterministically, not its live provider effect.

## Safety state

The shadow did not publish PAO tasks or mutate the routing bus. The raw
production circuit-state SHA-256 remained
`696ff1a166f403bc51ecda266048e9f0553fde709a6526b135b50d0d2e905c15`
before and after execution.

- circuit: `constraint_ordering::LWAR4`
- status: `open`
- reason: `candidate_rejected`
- reset count: 0
- audit: `healthy`

This is isolated adapter evidence. It is not a current-generation PAO routing
observation and cannot authorize circuit reset or live promotion.

Verdict:

`isolated_ordering_adapter_recovery_passed_correction_path_live_unexercised`

Portable evidence is stored in
`benchmarks/lwar4-ordering-recovery-evidence-v1.json`.

PAO v1.4.0 subsequently added the `recovery_shadow` routing contract described
in `docs/ROUTING_RECOVERY_SHADOW.md`. The contract creates a safe path for a
future current-generation PAO panel behind the open circuit. It does not
retroactively convert this adapter-only run into routing evidence.
