# REVIEW-PAOADP

## Scope

Review the PAO ADP design, implementation contract, and recovery semantics.

## Findings

### Passed areas

- ADP correctly treats the watcher as deterministic I/O and the LWAR session as the true repeating actor.
- Slot reuse is protected by `generation` and `instance_id`.
- Lease-based task recovery fits the file-bus model.
- OA and LWAR responsibilities are clearly separated.

### Risks to keep watching

1. Result replay approval must remain generation-aware.
2. Archive growth may require pruning or rotation policy.
3. Cross-runtime capability routing is still only partially specified.

## Verdict

`passed_with_followups`

## Follow-up Actions

- strengthen duplicate-result handling
- define archive retention policy
- refine runtime capability scoring for OA routing
