---
name: pgf
description: "PGF (PPR/Gantree Framework) — AI-native design and execution framework. Supports architecture design, work planning, autonomous execution, verification, discovery, and creation. Triggers: design, plan, execute, verify, discover, create, architecture, work breakdown, PGF, Gantree, PPR."
user-invocable: true
argument-hint: "design|plan|execute|verify|full-cycle|loop|discover|create [project-name|start|cancel|status]"
---

# PGF (PPR/Gantree Framework) v2.6

> If PG is the language, PGF is the library. It standardizes recurring AI work patterns such as design, execution, verification, discovery, and creation.

## Dependency on PG

PGF inherits PG notation. Gantree syntax, PPR syntax (`AI_`, `AI_make_`, pipelines, `[parallel]`, `acceptance_criteria`, convergence loops, failure strategy), type conventions, the atomic-node 15-minute rule, and status codes are all owned by PG. PGF adds execution modes, WORKPLAN and POLICY documents, status JSON files, phase transitions, and session learning.

PG's enforcement caveat also applies here: discipline is the quality gate.

## What Execution Means

PGF has no separate execution engine. WORKPLAN, POLICY, status files, and modes are conventions. The actual runtime is the AI that interprets them, chooses nodes, implements work, verifies outcomes, and updates state.

This means PGF is valuable as an operating discipline, not as a hidden piece of automation.

## Core Loop

```text
1. DECOMPOSE  break the design into atomic nodes
2. for each node in dependency order:
       EXECUTE  implement the node
       GATE     verify it with tests, contracts, or review
                 pass -> status=done
                 fail -> redesign or status=blocked
3. VERIFY     cross-check the whole result from three perspectives
```

Keep nodes small. Gate every node.

## Execution Modes

### Tier 1 — Core

| Mode | Trigger | Action |
|------|---------|--------|
| `design` | "design", "architecture" | produce `DESIGN-{Name}.md` with Gantree and PPR |
| `plan` | "plan", "WORKPLAN" | convert DESIGN into `WORKPLAN-{Name}.md` plus POLICY |
| `execute` | "implement", "execute" | run nodes in WORKPLAN order |
| `verify` | "verify", "cross-check" | run acceptance, quality, and architecture checks |
| `full-cycle` | "full cycle" | chain design, plan, execute, and verify |
| `micro` | "quickly", "simple" | zero-overhead path for small tasks, with automatic promotion when needed |

`design --analyze` reverse-engineers an existing system into PG form.

### Tier 2 — Discovery

| Mode | Trigger | Action |
|------|---------|--------|
| `discover` | "discover", "idea" | run idea discovery with multiple personas |
| `create` | "create autonomously" | chain discover, design, plan, execute, and verify |

### Tier 3 — Advanced

| Mode | Trigger | Use case |
|------|---------|----------|
| `loop` | "loop", "auto-run" | unattended iteration over nodes |
| `delegate` | "delegate" | AI-to-AI handoff when specialization or parallelism is needed |
| `review` | "review" | iterative improvement of existing artifacts |
| `evolve` | "evolve", "self-improve" | identify and close capability gaps |

## Mode Selection

```text
<= 3 nodes          -> inline PG Level 1, no PGF mode needed
<= 10 nodes         -> micro
new feature/system  -> design -> plan -> execute -> verify
idea-first work     -> create
long unattended run -> loop
parallel specialization -> delegate
artifact refinement -> review
```

## Scale Detection

| Scale | Rule | Strategy |
|-------|------|----------|
| Level 1 | <= 3 nodes | inline natural language |
| Level 2 | 4-10 nodes | Gantree plus compact comments |
| Level 3 | 11-30 nodes | full DESIGN, WORKPLAN, and status JSON |
| Large | > 30 nodes or decomposed tree | split modules plus PGXF index |
| Multi-agent | specialized parallel branches | delegate |

## File Layout

```text
<root>/.pgf/
    DESIGN-{Name}.md
    WORKPLAN-{Name}.md
    status-{Name}.json
```

PGF adds three status codes for delegation:

| Status | Meaning |
|---|---|
| `(delegated)` | sent to another agent |
| `(awaiting-return)` | waiting for remote completion |
| `(returned)` | remote result received and pending integration |

## Verification

Verification uses three perspectives:

1. **Acceptance**: re-check `acceptance_criteria`
2. **Code quality**: inspect reuse, cleanliness, and efficiency
3. **Architecture**: compare the design tree to the implementation structure

Verdicts:

- `passed`
- `rework`
- `blocked`

Rework targets only the affected subtree.

## Full-Cycle Transitions

| Transition | Condition | Failure path |
|------------|-----------|--------------|
| discover -> design | idea selection succeeds | abort if nothing is chosen |
| design -> plan | design completion criteria are met | continue design |
| plan -> execute | WORKPLAN and status files exist | report error |
| execute -> verify | all nodes are terminal | continue execute |
| verify -> complete | verification passes | rework or report |

## Session Learning

- load patterns from `.pgf/patterns/` at session start
- write outcomes to `.pgf/sessions/{id}.outcome.json` at session end
- periodically re-aggregate recurring successes and blockers

## Reference Files

Load only the mode-specific reference file you need:

- `reference/pgf-format.md`
- `reference/analyze-reference.md`
- `reference/workplan-reference.md`
- `reference/verify-reference.md`
- `reference/cycle-reference.md`
- `reference/design-review-reference.md`
- `reference/delegate-reference.md`
- `reference/micro-reference.md`
- `reference/session-learning-reference.md`
- `reference/review-reference.md`
- `reference/evolve-reference.md`
- `loop/loop-reference.md`
- `discovery/discovery-reference.md`

## Checklist

- Execute: every WORKPLAN node is terminal, status JSON matches the plan, blocked nodes have reasons.
- Verify: acceptance is re-checked, code quality is reviewed, design and implementation are compared, verdict is recorded, and rework stays scoped.
- Full-cycle: transition conditions are respected, rework does not reset the whole plan, interrupted runs can resume.
- Delegation and session: use `AI_make_` only where justified, consider micro mode for small work, define authority bounds when delegating, keep chain depth bounded, and record session outcomes.
