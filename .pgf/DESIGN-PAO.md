# DESIGN-PAO

PAO // Persistent Agent Orchestration architecture (in-progress) @v:0.2
    IdentityModel // Stable external identities for heterogeneous runtimes (done)
    TransportAbstraction // Replaceable message transport contract (in-progress)
        FileTransport // Local filesystem MVP transport (done)
        FutureTransportAdapters // MCP, SQLite, Redis, or IPC transports (designing)
    OrchestrationPlane // Goal decomposition, routing, and recovery (in-progress)
        GoalAnalysis // Interpret user objective and constraints (in-progress)
        TaskPlanning // Produce TaskContract nodes and dependency order (in-progress)
        RuntimeSelection // Pick the best LWAR for each task (done)
        RecoveryPolicy // Retry, reassign, or dead-letter failed tasks (done)
    ValidationPlane // Result verification and integration (in-progress)
        EvidenceVerification // Re-check commands, tests, and artifacts (in-progress)
        CrossReview // Optional verification by another runtime (designing)
        FinalIntegration // Assemble final outcome and report (designing)
    StatePlane // Durable lifecycle and execution history (in-progress)
        RegistryState // LWAR registration and lifecycle state (done)
        TaskState // Task lifecycle and status tracking (done)
        LeaseState // Claim leases and stale recovery rules (done)
        AuditState // Logs and archived payloads (done)
    RuntimePlane // Actual heterogeneous runtimes (in-progress)
        LWARSession // User-started long-running runtime session (done)
        ADPLoop // Watch, execute, submit, repeat (done)
        RuntimeAdapters // Runtime-specific metadata and capability mapping (designing)

```python
def persistent_agent_orchestration(goal: Goal) -> Outcome:
    plan = AI_decompose_goal(goal)
    routed_tasks = AI_route_tasks(plan, registry_state, runtime_capabilities)

    for task in routed_tasks:
        publish_task(task)
        monitor_progress(task)
        result = collect_result(task)
        if not verify_result(result, task.completion_criteria):
            task = recover_or_reassign(task, result)

    return integrate_results(routed_tasks)

    # acceptance_criteria:
    #   - external runtime identities remain provider-neutral
    #   - transport can change without rewriting orchestration semantics
    #   - every task has explicit verification before approval
    #   - stale identities and stale leases are recoverable
```
