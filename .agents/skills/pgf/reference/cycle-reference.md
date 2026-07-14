# Full-Cycle Reference

## Purpose

This reference defines the standard `design -> plan -> execute -> verify` loop used by `full-cycle` and reused by `create`.

## Phase Contract

| Phase | Output | Gate |
|------|--------|------|
| design | `DESIGN-{Name}.md` | decomposition is clear, executable, and verifiable |
| plan | `WORKPLAN-{Name}.md` and `status-{Name}.json` | execution order and policy are explicit |
| execute | implementation artifacts and updated status | every node reaches a terminal state |
| verify | verdict plus rework target if needed | acceptance, quality, and architecture checks |

## Review Extension

If `--with-review[=N]` is set, insert an optional design review gate between `design` and `plan`.

- default max review iterations: `1`
- pass condition: `Critical=0 AND High<=2`
- failure path: revise design and retry
- if the limit is reached: report unresolved issues and ask for user acknowledgment

This is an optional gate. Default `full-cycle` does not require it.

## Rework Rule

Never reset the entire plan because one verification step fails. Roll back only the affected subtree and preserve completed nodes outside that scope.

## Status Extension

The status JSON may add:

- `review_iterations`
- `unresolved_issues`
- phase metadata needed to resume a suspended cycle

Existing keys should remain stable.

## Transition Table

| Transition | Condition | Failure action |
|------------|-----------|----------------|
| review -> plan | critical count is zero and high issues are acceptable | revise design or stop for user acknowledgment |
| review -> blocked | iteration limit exceeded and user rejects proceeding | report unresolved issues |
| execute -> verify | all nodes are terminal | continue execution |
| verify -> complete | verdict is `passed` | finish |
| verify -> execute | verdict is `rework` | re-run only the affected subtree |

## Resume Rule

If the session stops, resume from the last recorded phase instead of rebuilding the entire cycle.
