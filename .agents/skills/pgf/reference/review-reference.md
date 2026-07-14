# Review Mode — Iterative Review and Improvement Specification

> Review mode repeatedly inspects existing artifacts such as documents, designs, skills, and code, then revises and re-verifies them. Unlike `design --analyze`, which reverse-engineers code into DESIGN form, review mode improves artifacts that already exist.

## 1. Overview

### Purpose

- identify inconsistency, omission, ambiguity, and improvement opportunities systematically
- prioritize issues, implement fixes, and verify them
- repeat until issues are exhausted

### When to Use

| Situation | Example |
|-----------|---------|
| documentation improvement | review PG or PGF skill docs |
| design validation | check internal consistency of `DESIGN.md` |
| skill hardening | fill missing capabilities in an existing skill |
| code review | inspect implementation quality, security, and performance |
| cross-verification | ensure consistency across multiple documents |

## 2. Commands

| Command | Action |
|---------|--------|
| `/PGF review {target}` | inspect a target file or directory closely |
| `/PGF review {target} --scope {files}` | limit review to selected files |
| `/PGF review {target} --max-cycles N` | repeat at most `N` times |

## 3. Execution Flow

```python
def review_cycle(target: str, scope: list[str] = None, max_cycles: int = 10) -> ReviewResult:
    cycle = 0
    all_fixes = []

    while cycle < max_cycles:
        cycle += 1
        issues = analyze(target, scope)
        if not issues:
            break

        prioritized = prioritize_issues(issues)
        fixes = implement_fixes(prioritized)
        all_fixes.extend(fixes)
        remaining = verify_fixes(target, fixes)

        report_cycle(cycle, len(issues), len(fixes), len(remaining))
        if not remaining:
            break

    return ReviewResult(
        cycles=cycle,
        total_issues=len(all_fixes),
        status="passed" if cycle < max_cycles else "max_cycles_reached",
    )
```

## 4. Analysis Framework

```python
def analyze(target: str, scope: list[str]) -> list[Issue]:
    content = read_all(target, scope)

    [parallel]
        consistency = AI_check_internal_consistency(content)
        completeness = AI_check_completeness(content)
        clarity = AI_check_clarity(content)
        accuracy = AI_check_accuracy(content)
        improvements = AI_identify_improvements(content)

    if len(scope or [target]) > 1:
        cross = AI_check_cross_consistency(content)
        return merge_deduplicate(consistency, completeness, clarity, accuracy, improvements, cross)

    return merge_deduplicate(consistency, completeness, clarity, accuracy, improvements)
```

Issue format:

```python
Issue = {
    "id": str,
    "location": str,
    "type": str,
    "impact": str,
    "description": str,
    "suggestion": str,
}
```

## 5. Prioritization

```python
def prioritize_issues(issues: list[Issue]) -> list[Issue]:
    priority_order = {
        ("high", "fix"): 1,
        ("high", "improve"): 2,
        ("medium", "fix"): 3,
        ("high", "add"): 4,
        ("medium", "improve"): 5,
        ("medium", "add"): 6,
        ("low", "fix"): 7,
        ("low", "improve"): 8,
        ("low", "add"): 9,
    }
    return sorted(issues, key=lambda i: priority_order.get((i.impact, i.type), 10))
```

## 6. Progress Report Format

```text
[PGF REVIEW] Cycle 1 | target: PG/SKILL.md
  Analyzed: 17 issues found (6 fix, 7 improve, 4 add)
  Implemented: 11 fixes
  Remaining: 1 (deferred)

[PGF REVIEW] Cycle 2 | re-verification
  Analyzed: 0 new issues
  Judgment: passed

[PGF REVIEW] === Complete ===
  Cycles: 2
  Total fixes: 11
  Files modified: 3
  Status: passed
```

## 7. Relationship with Other Modes

| Mode | Relationship |
|------|-------------|
| `design --analyze` | reverse-engineers code into DESIGN form; review improves existing artifacts |
| `verify` | validates implementation; review can happen before or after implementation |
| `design-review` | design-only pre-plan gate; review is a general iterative improvement loop |
