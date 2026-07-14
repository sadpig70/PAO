# Evolve Mode — Self-Improvement Cycle

## Purpose

`evolve` analyzes the agent's current capability gaps, designs changes, implements them, verifies them, and records the outcome.

## Goals

- discover capability gaps autonomously
- design and implement targeted improvements
- verify each improvement independently
- record results for future sessions
- stop naturally when the system stabilizes

## Command Surface

| Command | Action |
|---------|--------|
| `/PGF evolve` | start the self-improvement loop |
| `/PGF evolve --cycles N` | stop after `N` cycles |
| `/PGF evolve status` | report current progress |
| `/PGF evolve stop` | stop the loop |

## Execution Loop

```python
def evolve_loop() -> None:
    while True:
        gaps = capability_audit()
        if not gaps:
            break

        gap = prioritize_gap(gaps)
        design = AI_design_evolution(gap)
        implementation = implement_evolution(design)
        verification = verify_evolution(implementation)
        record_evolution(gap, implementation, verification)

        if stabilization_detected():
            break
```

## Capability Audit

Think across six axes:

- reasoning quality
- tool use
- workflow automation
- memory and continuity
- integration depth
- verification discipline

## Constraints

```python
EvolutionPolicy = {
    "file_based_only": True,
    "pgf_consistency": True,
    "independently_verifiable": True,
    "record_required": True,
    "no_destructive_changes": True,
}
```

## Stabilization Rules

Stop when one of these is true:

- no meaningful gaps remain
- remaining gaps are not solvable with current tools
- recent improvement impact is consistently declining

## Logging

Append one entry per evolution item with:

- gap addressed
- implementation summary
- changed files
- verification result
- impact gained

## Relationship with Other Modes

| Mode | Difference |
|------|------------|
| `review` | improves existing artifacts; `evolve` creates new capability |
| `create` | creates outward-facing work; `evolve` creates inward-facing capability |
| `full-cycle` | general workflow; `evolve` is specialized for self-improvement |
| `discover` | explores external ideas; `evolve` discovers internal gaps |
