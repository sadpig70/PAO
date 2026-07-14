# Design Review Protocol

## Purpose

Validate design quality from multiple perspectives before moving from DESIGN to PLAN. Catching problems at design time is far cheaper than rework after implementation.

## When to Trigger

- after the four `design` completion criteria are satisfied and the user explicitly asks for it
- in `full-cycle` or `create` only when `--with-review[=N]` is present, right before the design-to-plan transition
- when the user requests `/PGF design-review` or `/PGF review --design`

The default full-cycle path does not include this gate unless explicitly requested.

## Loop Contract

`full_cycle()` enters through `run_design_review_loop(design_path, max_iterations, status_path)` and guarantees:

1. at most `N` review-revise cycles, default `1`
2. immediate pass when `Critical=0 AND High<=2`
3. `AI_revise_design(design_path, issues)` on failed review
4. `status="needs_user_ack"` plus `unresolved_issues` if the loop exceeds `N`
5. status JSON adds only `review_iterations` and `unresolved_issues`
6. Level 1 and `micro` tasks skip this gate automatically

## 3-Perspective Review

Use three lightweight reviewers from the eight-persona set:

| Reviewer | Persona Base | Focus |
|----------|-------------|-------|
| **Feasibility Reviewer** | P5 (Field Operator) | implementation feasibility, technology choices, complexity |
| **Risk Reviewer** | P7 (Contrarian Critic) | fatal weaknesses, hidden assumptions, scaling risk |
| **Architecture Reviewer** | P8 (Convergence Architect) | structural consistency, module coupling, evolvability |

## Review Process

```python
def design_review(design_path: str) -> ReviewResult:
    design = Read(design_path)

    [parallel]
        feasibility = Agent(...)
        risk = Agent(...)
        architecture = Agent(...)

    if all_pass([feasibility, risk, architecture]):
        return ReviewResult(status="APPROVED", notes=aggregate_notes)
    concerns = collect_concerns([feasibility, risk, architecture])
    return ReviewResult(status="REVISE", concerns=concerns)
```

## Result Actions

| Result | Action |
|--------|--------|
| 3/3 PASS | proceed to PLAN |
| 2/3 PASS | address concerns, then proceed if none are critical |
| 1/3 or 0/3 PASS | revise DESIGN before continuing |

## Integration with PGF Modes

- `design` -> manual `design-review` -> `plan`
- `full-cycle` -> optional automatic trigger after design completion
- `create` -> optional automatic trigger in autonomous mode

## Lightweight Mode

For Level 1-2 tasks with ten nodes or fewer, skip multi-agent review. A single self-review is enough:

- read the design
- ask whether P7 would find a fatal flaw
- fix it if yes, continue if no
