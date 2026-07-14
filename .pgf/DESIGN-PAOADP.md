# DESIGN-PAOADP

PAOADP // ADP-centered resident execution design (in-progress) @v:0.2
    RegistrationFlow // Self-registration and OA approval (done)
        RegistrationRequest // Publish runtime metadata request (done)
        IdentityAdoption // Adopt approved identity file (done)
    WatchLoop // Deterministic watch slice and agent-driven repetition (done)
        IdleSlice // Re-run watcher after idle timeout (done)
        StateWaitSlice // Re-run watcher when lifecycle is not executable (done)
        TaskSlice // Claim, execute, and complete a task (done)
        ControlSlice // Handle ping, drain, cancel, and shutdown (done)
    ResultFlow // Normalize and submit results (done)
        DraftResult // Write result draft in work area (done)
        SubmitResult // Move result into outgoing contract flow (done)
        ArchiveResult // Store archived result payloads (done)
    RecoveryFlow // Protect against stale claims and stale identities (done)
        HeartbeatUpdate // Refresh liveness state (done)
        LeaseExpiry // Return expired tasks to incoming with retry budget (done)
        GenerationGuard // Reject stale-generation payloads (done)
        DuplicateResultGuard // Prevent blind replay approval (done)

```python
def adp(identity_file: Path) -> None:
    while True:
        event = watch(identity_file)
        if event.type in {"idle_timeout", "state_wait"}:
            continue
        if event.type == "task_received":
            result = AI_execute_task(event.task)
            submit_result(identity_file, event.task_id, result)
            continue
        if event.type == "control":
            if event.command == "shutdown":
                return
            handle_control(event)
            continue
        raise ADPError(event)

    # acceptance_criteria:
    #   - timeout exits do not terminate the LWAR session
    #   - a claimed task always ends with exactly one result contract
    #   - stale generation messages are rejected
    #   - OA can recover expired leases without corrupting current work
```
