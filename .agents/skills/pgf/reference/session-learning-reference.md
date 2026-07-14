# Session Learning — Cross-Session Strategy Evolution

> PGF v2.2 — Tracks what worked/failed across sessions to improve future execution.

---

## 1. Overview

PGF currently has no mechanism to learn from session outcomes. Each session starts from scratch. Session Learning adds:

- **Outcome Recording**: After each PGF execution, record what succeeded, what failed, and why
- **Pattern Recognition**: Identify recurring blockers, successful strategies, and common rework causes
- **Strategy Adaptation**: Use past outcomes to inform future POLICY defaults, node ordering, and agent selection

---

## 2. Session Outcome Record

After each PGF execution (full-cycle, execute, loop), automatically generate:

```python
SessionOutcome = {
    "session_id": str,
    "project": str,
    "mode": str,                    # "full-cycle", "execute", "micro", etc.
    "started_at": iso8601,
    "ended_at": iso8601,
    "duration_minutes": float,
    "nodes": {
        "total": int,
        "done": int,
        "blocked": int,
        "reworked": int,
    },
    "rework_log": [                 # Why verify failed
        {"node": str, "reason": str, "cycle": int}
    ],
    "blocked_log": [                # Why nodes were blocked
        {"node": str, "blocker": str}
    ],
    "successful_patterns": [        # What worked well
        {"pattern": str, "context": str}
    ],
    "agent_delegations": [          # Inter-agent handoffs
        {"agent": str, "task": str, "outcome": str, "duration_ms": int}
    ],
    "policy_overrides": dict,       # Any POLICY changes made during execution
}
```

### Storage

```
.pgf/
    sessions/
        {session_id}.outcome.json
    patterns/
        successful_strategies.json    # Accumulated patterns
        common_blockers.json          # Recurring failure modes
        policy_adaptations.json       # POLICY auto-adaptation rules
```

### Pattern File Schemas

**successful_strategies.json**:
```json
{
  "version": "1.0",
  "strategies": [
    {
      "id": "S001",
      "pattern": "description of what worked",
      "context": "when/where it was effective",
      "confidence": 0.85,
      "sessions": ["session-id-1"]
    }
  ]
}
```

**common_blockers.json**:
```json
{
  "version": "1.0",
  "blockers": [
    {
      "id": "B001",
      "pattern": "description of failure mode",
      "frequency": 1,
      "severity": "high|medium|low",
      "mitigation": "how to avoid",
      "sessions": ["session-id-1"]
    }
  ]
}
```

**policy_adaptations.json**:
```json
{
  "version": "1.0",
  "adaptations": [
    {
      "trigger": "condition description",
      "adaptation": "POLICY field change",
      "reason": "why this adaptation",
      "source_blocker": "B001"
    }
  ]
}
```

---

## 3. Pattern Accumulation

```python
def accumulate_patterns(outcomes: list[SessionOutcome]) -> PatternDatabase:
    """Extract recurring patterns from session history"""

    strategies = AI_identify_successful_patterns(outcomes)
    # e.g., "parallel agent dispatch for channel adapters saves 60% time"

    blockers = AI_identify_common_blockers(outcomes)
    # e.g., "clippy warnings in cross-crate changes — always run clippy per-crate"

    rework_causes = AI_identify_rework_causes(outcomes)
    # e.g., "missing acceptance_criteria leads to 40% rework rate"

    return PatternDatabase(strategies, blockers, rework_causes)
```

---

## 4. Strategy Adaptation

At the start of each PGF execution, load session history and adapt:

```python
def adapt_strategy(
    task: str,
    pattern_db: PatternDatabase,
    default_policy: Policy,
) -> AdaptedPolicy:
    """Adapt POLICY defaults based on past session outcomes"""

    similar_sessions = pattern_db.find_similar(task)

    if similar_sessions:
        # Adjust based on past performance
        policy = default_policy.clone()

        avg_rework_rate = mean([s.nodes.reworked / s.nodes.total for s in similar_sessions])
        if avg_rework_rate > 0.3:
            policy.max_verify_cycles += 1  # More verification if high rework history

        common_blockers = pattern_db.blockers_for(task)
        if "timeout" in common_blockers:
            policy.max_retry += 1  # More retries if timeouts common

        return AdaptedPolicy(policy, adaptations_applied=True)

    return AdaptedPolicy(default_policy, adaptations_applied=False)
```

---

## 5. Integration Points

| When | Action |
|---|---|
| PGF execution completes | Auto-generate `{session_id}.outcome.json` |
| PGF execution starts | Load `patterns/` and adapt POLICY |
| Every 10 sessions | Re-accumulate patterns from all outcomes |
| `/PGF status` | Show learning statistics |

---

## 6. Progress Report

```text
[PGF LEARN] Session outcome recorded | 12/15 done, 2 blocked, 1 reworked
[PGF LEARN] Loaded 8 patterns from 23 past sessions
[PGF LEARN] Adapted POLICY: max_verify_cycles 2→3 (high rework history)
```
