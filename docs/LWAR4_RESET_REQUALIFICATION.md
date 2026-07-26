# LWAR4 reset and fresh-evidence requalification

## Objective

Decide whether the open `constraint_ordering::LWAR4` circuit can be reset
without weakening the sticky fallback contract, then require fresh post-reset
evidence before one production canary.

## Sealed campaign

The campaign is defined by:

- `benchmarks/lwar4-reset-requalification-suite-v1.json`
- `benchmarks/lwar4-reset-requalification-preregistration-v1.json`

The suite contains 27 unique prompts:

- 12 paired current-generation recovery/control tasks
- 13 post-reset LWAR4 ordinary shadows
- one production canary
- one conditional fallback probe

Expected answers are hashed before execution and are never supplied to a
provider. Every task has `permissions.write=[]` and
`permissions.network=false`.

## Conditional gates

Reset is allowed only when LWAR1 and LWAR4 both pass 12/12 paired tasks, all
telemetry is complete, audit is healthy, active work is empty, and the open
circuit bytes remain unchanged during recovery.

After reset, pre-reset LWAR4 observations are fenced by `reset_at`. Thirteen
fresh accepted ordinary shadows are required because the incumbent already has
13 accepted ordering observations; equal perfect sample sizes yield equal
Wilson lower bounds. Any rejection opens the circuit and stops the production
branch.

One production canary is allowed only when live selection reports
`confidence_qualified_live`. Acceptance must leave the circuit closed.
Rejection must open the sticky circuit and is followed by exactly one incumbent
fallback probe.

## Honest claim boundary

The campaign can prove current-generation behavioral recovery and safe
production routing. It does not erase the earlier rejection, prove the live
effect of a correction path that was never triggered, or convert post-hoc token
opportunity into a held-out routing-benefit claim.

## Campaign history

The sealed v1 recovery gate closed negative:

- LWAR1: 12/12 accepted
- LWAR4: 8/12 accepted
- telemetry: 24/24 complete
- circuit fingerprint: unchanged
- audit: healthy
- active claims and leases: zero

No reset or post-reset task ran. Three LWAR4 failures exposed a verifier
coverage gap for `position`, `before`, and `not-adjacent` constraints; one task
still failed after the existing two-call correction path. The v1 evidence is
preserved in
`benchmarks/lwar4-reset-requalification-evidence-v1.json`.

The answer-key-free finite verifier now checks every constraint form generated
by the suite. A v2 campaign must use a fully nonoverlapping prompt set, bind the
v1 closed-negative evidence, and execute at most once. v1 tasks are never
replayed.
