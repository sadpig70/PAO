# DELEGATE Mode — First-Class AI-to-AI Collaboration

> Co-designed by Claude Opus 4.6 + Kimi K2.5 (PGF v2.2)
> Enables multi-agent collaboration with typed contracts and trust verification.

---

## 1. Overview

DELEGATE mode formalizes AI-to-AI task handoff. Instead of improvising agent prompts, the delegating AI packages context into a **PG TaskSpec** with explicit authority bounds, acceptance criteria, and return contracts.

### When to Delegate

```python
def should_delegate(task: GantreeNode, agent_state: AgentState) -> bool:
    """Determine if a task should be delegated to another agent"""
    # Capability gap: task requires skills the current agent lacks
    if task.required_skills - agent_state.skills:
        return True
    # Load balancing: queue is too deep
    if agent_state.queue_depth > agent_state.threshold:
        return True
    # Parallel opportunity: [parallel] block with independent subtasks
    if task.is_parallel_block and len(task.children) >= 3:
        return True
    return False
```

---

## 2. Delegation Protocol

### 2.1 Context Packaging

Before delegation, compress context to fit the target agent's window:

```python
def package_context(
    task: GantreeNode,
    design: Optional[Path],
    max_tokens: int = 4096,
) -> DelegationPacket:
    """Package task context for handoff"""
    packet = DelegationPacket(
        task_spec=generate_task_spec(task),           # PG TaskSpec
        decision_log=extract_relevant_decisions(task), # Why decisions were made
        dependencies=resolve_dependencies(task),       # What must be read first
        authority=AuthorityBounds(
            can_create=task.create_scope,              # Files that can be created
            can_modify=task.modify_scope,              # Files that can be modified
            forbidden=task.forbidden_ops,              # Operations not allowed
        ),
    )
    return AI_compress_context(packet, token_limit=max_tokens)
```

### 2.2 Handshake

```python
def delegate_handshake(
    target_agent: AgentRef,
    packet: DelegationPacket,
) -> DelegationResult:
    """Execute delegation with handshake verification"""

    # 1. Verify target agent capabilities
    assert target_agent.skills >= packet.task_spec.required_skills

    # 2. Transmit TaskSpec
    response = target_agent.execute(packet)

    # 3. Validate return
    if not validate_return(response, packet.task_spec.acceptance_criteria):
        return DelegationResult(status="failed", reason="acceptance_criteria_not_met")

    # 4. Integrate result
    merge_into_workplan(response, packet.task_spec.node)

    return DelegationResult(status="done", response=response)
```

### 2.3 Delegation Chain Tracking

Prevent infinite delegation loops:

```python
DelegationChain = {
    "chain": [
        {"agent": "claude-opus", "task": "implement_adapters", "depth": 0},
        {"agent": "subagent-1", "task": "discord_adapter", "depth": 1},
    ],
    "max_depth": 3,  # Default 3. Override via POLICY.delegation_max_depth
    "cycle_check": True,  # A→B→A detected and blocked
}
```

---

## 3. Status Codes

New Gantree status codes for delegation:

| Status | Meaning |
|---|---|
| `(delegated)` | Handed off to another agent |
| `(awaiting-return)` | Delegation sent, waiting for result |
| `(returned)` | Result received, pending integration |

---

## 4. Authority Bounds

Every delegation must explicitly state what the delegate can and cannot do:

```python
AuthorityBounds = {
    "can_create": ["src/adapters/*.rs"],          # File patterns
    "can_modify": ["src/adapters/mod.rs", "Cargo.toml"],
    "forbidden": ["git push", "delete files outside scope"],
    "design_modify_scope": ["impl"],              # From POLICY
}
```

---

## 5. Failure Handling

```python
def handle_delegation_failure(
    result: DelegationResult,
    packet: DelegationPacket,
    retry_count: int,
) -> Action:
    """Handle failed delegation"""
    if retry_count < 2:
        # Retry with refined context
        refined = AI_refine_task_spec(packet, result.failure_reason)
        return Action.Retry(refined)
    else:
        # Escalate: mark as blocked, report to user
        return Action.Escalate(
            node=packet.task_spec.node,
            reason=result.failure_reason,
            attempts=retry_count,
        )
```

---

## 6. Invocation

Delegation is not a standalone mode — it is triggered automatically during `execute` or `full-cycle` when `should_delegate()` returns `True`, or explicitly via:

```
/PGF delegate "Implement Discord adapter" --to kimi --authority "can_create:src/adapters/discord.rs"
```

---

## 7. Integration with agent-protocol.md

DELEGATE mode uses `agent-protocol.md`'s TaskSpec format as the wire format. The additions are:

- `DelegationPacket` wraps TaskSpec with authority bounds + decision log
- `DelegationChain` metadata prevents cycles
- New status codes `(delegated)`, `(awaiting-return)`, `(returned)` in Gantree
