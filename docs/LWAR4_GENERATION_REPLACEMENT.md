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
