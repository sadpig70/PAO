# LWAR4 generation replacement

## State

The failed LWAR4 generation 1 has been administratively retired through the
PAO v1.4.2 stale idle retirement contract. Its open
`constraint_ordering::LWAR4` circuit and negative evidence remain preserved.
Retirement does not reset or qualify the circuit.

The next provider session must:

1. truthfully report a provider/model profile that differs from generation 1;
2. request the exact `LWAR4` slot;
3. receive generation 2 through OA reconciliation;
4. adopt atomically with `response --resident`; and
5. bind its identity and adapter contract in a final preregistration before any
   calibration provider call.

## Sealed calibration material

`benchmarks/lwar4-generation2-calibration-suite-v1.json` contains 27 fresh,
nonoverlapping finite ordering tasks:

- 12 paired recovery tasks;
- 13 possible post-reset ordinary shadows;
- one possible production canary; and
- one possible fallback probe.

Its canonical SHA-256 is
`b253145eb965d9d7fb27d98d1b1fa99c20b91104d9df481ca738e08d19c222a8`.

The suite is provider-neutral, answer-key-free at execution, limited to one
campaign execution, and bound to the terminal generation-1 evidence. Identity,
provider adapter, telemetry contract, and live circuit fingerprint remain
explicitly pending. They must be sealed after generation-2 registration and
before the first provider call.

No calibration task is authorized while
`identity_and_adapter_binding=pending_before_provider_execution`.

## Generation-2 binding

After the replacement identity is adopted, build and commit
`benchmarks/lwar4-generation2-calibration-preregistration-v1.json`. It binds
the exact identity tuple and profile, the resident begin/complete adapter
contract, the sealed suite, and the current open-circuit bytes.

Only exact runtime-reported token counts may enter routing observations. If
Qwen does not expose an exact count, record it as unavailable, preserve the
open circuit, and stop before reset. Correctness evidence remains valid, but it
does not authorize production promotion or a token-efficiency claim.

The adopted profile proves a different adapter and provider family. Because the
session reports `Unreported Model`, it does not by itself prove exact-model
heterogeneity.

## Campaign result

The committed generation-2 campaign stopped at RR01. Its objective answer was
correct, but the provider reported using a prohibited Python verification tool
and reported token telemetry as unavailable. OA therefore recorded a semantic
rejection, preserved the open circuit, and did not execute RR02-RR12, reset,
post-reset shadows, production canary, or fallback.

The terminal evidence is
`benchmarks/lwar4-generation2-calibration-evidence-v1.json`.

## Generation 3 campaign result (Kimi)

A generation-3 Kimi Code CLI provider (adapter `kimi_cli`, vendor `moonshot`,
model `kimi-for-coding`) replaced the retired generation 2. It registered the
exact `LWAR4` slot, received generation 3, adopted, and executed all 12 sealed
recovery tasks (RR01-RR12) through the machine-enforced Kimi host adapter
(`kimi-run`), reusing the generation-2 sealed suite.

Every executed task was fully contract-compliant: **12/12 with zero tool calls
and exact input/output/total token telemetry** — the tool-use and
telemetry failures that stopped generation 2 did not recur. The answer-key-free
finite verifier accepted **11/12**; RR06 was a genuine objective miss (a valid
constraint ordering was not produced under a contract-compliant receipt). RR02's
first attempt was a transient host process crash (empty answer, non-zero exit)
and was re-measured to a correct answer; an incorrect answer is never
re-attempted.

Because the recovery gate requires 12/12, it did not pass. OA recorded the
result, executed no circuit reset, and preserved the open
`constraint_ordering::LWAR4` circuit unchanged (before == after). The failure
mode shifted from generation 2's contract violation to a single reasoning miss,
and provider-family heterogeneity (`moonshot`) was observed, but production
qualification remains unauthorized.

The terminal evidence is
`benchmarks/lwar4-generation3-calibration-evidence-v1.json`
(preregistration `benchmarks/lwar4-generation3-calibration-preregistration-v1.json`).
