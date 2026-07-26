# Routing Recovery Shadow

## Purpose

`recovery_shadow` collects current-instance/current-generation evidence from
one explicitly named LWAR while its alias/class circuit remains open. It closes
the evidence deadlock without allowing live traffic, automatic promotion, or
automatic circuit reset.

Ordinary `--routing-shadow` and `--routing-shadow-lwar-id` remain suppressed by
an open circuit.

## Publication

Use the same routing profile and canary policy that opened the circuit:

```bash
python "<PAO_SKILL>/scripts/oa.py" send \
  --auto \
  --routing-profile ROUTING_PROFILE.json \
  --routing-class constraint_ordering \
  --canary-policy CANARY_POLICY.json \
  --routing-recovery-shadow-lwar-id LWAR4 \
  --task-file READ_ONLY_TASK_DRAFT.json
```

The task draft must explicitly contain:

```json
{
  "permissions": {
    "read": [],
    "write": [],
    "network": false
  }
}
```

The selected LWAR must be currently eligible, the exact
`<task_class>::<LWARn>` circuit must be open, and its stored policy SHA-256
must match the supplied policy. The three shadow modes are mutually exclusive.

## Fail-closed boundary

For recovery publication, OA verifies the read-only permission structure before
it refreshes circuit state or writes a routing receipt, ledger entry, or mailbox
task. Missing permissions, nonempty writes, network access, invalid read paths,
unknown permission keys, and invalid artifact limits are rejected.

The receipt then binds:

- task class and `recovery_shadow` mode
- selected instance, generation, and registry version
- routing profile and matching canary policy
- complete verified observation set
- exact open-circuit state

## Validation and statistical isolation

Record the semantic decision and reported tokens through the ordinary strict
validation path:

```bash
python "<PAO_SKILL>/scripts/oa.py" validate \
  --task-id TASK_ID \
  --record \
  --decision accepted \
  --reason "objective grader passed" \
  --routing-reported-tokens 123
```

The resulting observation is replay-safe current-generation PAO evidence.
Selectors remove `recovery_shadow` rows before calculating class/global
confidence or promotion readiness. Circuit refresh also ignores them.
Therefore neither accepted nor rejected recovery panels can:

- route production traffic
- satisfy the automatic promotion floor
- open, close, or reset a circuit

An operator may use the isolated evidence in a separate reset-readiness review.
The existing reason-bound `routing-circuit-reset` command remains the only
state transition that can close the circuit. A reset is also an alias/class
promotion epoch watermark: ordinary observations at or before `reset_at` no
longer count for that candidate. Fresh ordinary shadows must rebuild confidence
before a production route can be selected.

## Verification contract

The integration suite proves:

- a matching open circuit routes the explicit current identity
- absent/mismatched circuits and policies fail closed
- unsafe permissions fail before bus mutation
- receipt, validation, and observation bindings survive replay validation
- recovery-only and pre-reset alias/class observations do not promote after a
  later reset
- recovery acceptance/rejection windows do not mutate circuit state
- master and generated runtime bundles remain byte-identical
