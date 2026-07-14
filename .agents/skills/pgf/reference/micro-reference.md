# MICRO Mode — Zero-Overhead Execution for Small Tasks

> Co-designed by Claude Opus 4.6 + Kimi K2.5 (PGF v2.2)
> Eliminates WORKPLAN friction for ≤10 node tasks.

---

## 1. Overview

MICRO mode bypasses WORKPLAN generation, POLICY block, and status JSON for small tasks. 70%+ of real-world agent tasks are small; full PGF overhead kills velocity.

### Entry Criteria

A task qualifies for MICRO mode when **all** conditions are met:

| Condition | Threshold |
|---|---|
| Node count | ≤ 10 |
| Tree depth | ≤ 3 |
| External dependencies | 0 |
| Estimated duration | ≤ 30 minutes |

### Automatic Promotion

If during MICRO execution any condition is violated (e.g., complexity grows beyond 10 nodes), **promote to full WORKPLAN** immediately:

```python
def check_micro_promotion(current_state: MicroState) -> bool:
    """Promote to full WORKPLAN if micro bounds exceeded"""
    if current_state.node_count > 10:
        return True  # promote
    if current_state.elapsed_minutes > 30:
        return True  # promote
    if current_state.discovered_dependencies > 0:
        return True  # promote
    return False  # stay micro
```

---

## 2. Execution Flow

```python
def micro_execute(task_description: str) -> MicroResult:
    """Zero-overhead small task execution"""

    # 1. Inline task decomposition (no WORKPLAN file)
    nodes = AI_decompose_micro(task_description)
    assert len(nodes) <= 10, "promote to full WORKPLAN"

    # 2. Serial execution (no [parallel] in micro mode)
    results = []
    for node in nodes:
        result = AI_execute_node(node)
        results.append(result)

        # Check promotion
        if check_micro_promotion(current_state):
            return promote_to_workplan(nodes, results)

    # 3. Minimal verification (max 3 checks)
    verify_result = micro_verify(results, checks=[
        "functional_correctness",
        "no_regression",
        "style_compliance",
    ])

    # 4. Archive to session log (not status JSON)
    archive_micro_log(task_description, nodes, results, verify_result)

    return MicroResult(status="done", results=results)
```

---

## 3. Inline Status Format

MICRO mode uses **in-memory** status tracking, not status JSON files:

```python
MicroState = {
    "task": str,              # One-line description
    "nodes": [str],           # Simple list of node names
    "done": [str],            # Completed nodes
    "current": str,           # Currently executing
    "elapsed_minutes": float, # Wall clock
    "notes": [str],           # Optional observations
}
```

### Progress Report

```text
[PGF MICRO] task: Fix clippy warnings | 3/5 done | current: FixLifetimes
[PGF MICRO] ✓ Complete | 5/5 done | 8 minutes
```

---

## 4. Restrictions

| Rule | Reason |
|---|---|
| No `[parallel]` subtasks | Micro = serial simplicity |
| No sub-delegation | Prevent micro-within-micro |
| No DESIGN file | Inline decomposition only |
| No POLICY block | Fixed defaults |
| Max 3 verify checks | Quick diff, not full verification |
| Duration hard cap: 30 min | Promote if exceeded |

---

## 5. When to Use

| Situation | Mode |
|---|---|
| Bug fix (1-3 files) | MICRO |
| Add a single function | MICRO |
| Update config/docs | MICRO |
| Refactor rename | MICRO |
| Feature with dependencies | full-cycle |
| Multi-module change | full-cycle |
| Unknown scope | design --analyze first |

---

## 6. Invocation

```
/PGF micro "Fix clippy warnings in ocwr_daemon"
/PGF micro "Add serialization tests for UserConfig"
```

Or auto-detected when PGF estimates ≤10 nodes from the task description.
