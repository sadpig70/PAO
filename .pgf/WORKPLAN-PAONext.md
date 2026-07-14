# WORKPLAN-PAONext

> Source design: `_workspace/DESIGN-PAONext.md` (approved 2026-07-14)
> Mode: execute | All code, docs, and tests in English.

## POLICY

- backward_compat: existing 7 integration tests must pass unmodified
- schema_changes: additive only — no v1 field removal or meaning change
- forbidden: direct mailbox editing by OA/LWAR contracts stays forbidden
- docs_sync: skills and docs updated in the same change set
- max_verify_cycles: 2

## Nodes

```text
PAONext // PAO improvement execution (in-progress) @v:0.1
    P0_ReliabilityCore // enforce contract fields that exist but are not enforced (in-progress)
        RetryBudget // recover increments attempt, republishes updated payload (in-progress)
        DeadLetterQueue // dead/ dir + oa dead list/requeue commands (in-progress) @dep:RetryBudget
        DuplicateResultGuard // collect verifies identity tuple + ledger, quarantine/ (in-progress)
        TaskLeaseAlignment // effective lease = max(default, timeout_s + 30) (in-progress)
    P1_TaskStatePlane // OA-side task ledger and observability (in-progress)
        TaskLedger // var/tasks/{workflow_id}/{task_id}.json transitions (in-progress)
        LedgerHooks // send/collect/recover update the ledger (in-progress) @dep:TaskLedger
        HeartbeatMonitor // status computes staleness (default 120s) (in-progress)
        ValidateCommand // oa validate — mechanical checks + criteria checklist (in-progress) @dep:TaskLedger
    P1_CapabilityRouting // capability-based automatic routing (in-progress)
        CapabilityIndex // filter on slots' profile.capabilities (in-progress)
        LoadSignal // backlog + heartbeat scoring (in-progress)
        AutoRoute // oa send --auto --require-capability (in-progress) @dep:CapabilityIndex @dep:LoadSignal
    P1_TestHardening // close test blind spots (in-progress)
        RetryPathTests // attempt/dead-letter transitions (in-progress) @dep:P0_ReliabilityCore
        GuardTests // stale/duplicate quarantine, lease alignment (in-progress) @dep:P0_ReliabilityCore
        FlowTests // cancel, priority, tombstone window (in-progress)
    P2_Maintenance // operational upkeep (in-progress)
        ArchiveRetention // oa prune --older-than-days (in-progress)
        AuditLog // var/audit/events.jsonl append-only (in-progress)
    P2_TransportAbstraction // replaceable message plane (in-progress)
        TransportProtocol // Protocol for publish/claim/complete/lease/heartbeat (in-progress)
        FileTransportAdapter // file bus behind the protocol, behavior unchanged (in-progress) @dep:TransportProtocol
    P2_WorkflowDAG // multi-task orchestration (in-progress)
        DependsOnContract // task.depends_on gates publication on succeeded deps (in-progress) @dep:TaskLedger
        WorkflowStatus // oa workflow-status aggregation (in-progress) @dep:DependsOnContract
```

## Verification

- `python -m unittest discover -s tests -v` — all suites green
- `python -m py_compile pao_runtime/*.py scripts/*.py tests/*.py`
