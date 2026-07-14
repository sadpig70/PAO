# PGF File Format Specification

## 1. Overview

PGF documents are AI Parser-Free design/execution documents using the `.md` (Markdown) extension. Being Markdown-based, they render natively in all editors, GitHub, and CI/CD pipelines.

---

## 2. File Types and Naming Conventions

### Naming Conventions

All PGF documents follow the `{TYPE}-{ProjectName}.md` pattern.

```text
<project-root>/
    .pgf/
        DESIGN-{Name}.md          # System design (Gantree + PPR)
        WORKPLAN-{Name}.md        # Executable work plan
        status-{Name}.json        # Execution state tracking
```

**`{Name}` rules:**
- CamelCase (e.g., `EpigeneticPPR`, `DiscoveryEngine`, `PGFSkillCompletion`)
- Derived from project name or task name
- Multiple tasks can coexist in the same `.pgf/` directory

**Example:**
```text
.pgf/
    DESIGN-EpigeneticPPR.md
    WORKPLAN-EpigeneticPPR.md
    status-EpigeneticPPR.json
    DESIGN-DiscoveryEngine.md
    WORKPLAN-PGFSkillCompletion.md
    status-PGFSkillCompletion.json
```

### File Types

| File Pattern | Purpose | Required Components |
|-------------|---------|---------------------|
| `DESIGN-{Name}.md` | System design specification | Gantree tree + PPR def blocks |
| `WORKPLAN-{Name}.md` | Executable work plan | POLICY block + execution Gantree |
| `status-{Name}.json` | Execution state tracking | Per-node status + outputs + summary |
| `REVIEW-{Name}.md` | Review/audit report | Scope, findings, priority, next actions |

---

## 3. DESIGN-{Name}.md Structure

```
# {ProjectName} Design @v:X.Y

## Gantree

\```
RootNode // root description (designing) @v:1.0
    ModuleA // module A (designing)
        SubA1 // sub A1 (designing)
        SubA2 // sub A2 (designing)
    ModuleB // module B (designing) @dep:ModuleA
\```

## PPR

\```python
def module_a(input: InputType) -> OutputType:
    """Module A detailed implementation"""
    ...
\```
```

### Rules

- `## Gantree` section: entire system hierarchical structure
- `## PPR` section: def blocks for complex nodes (atomic nodes use inline)
- Version is managed via the `@v:` tag on the root node

---

## 4. WORKPLAN-{Name}.md Structure

```
# {ProjectName} Work Plan

## POLICY

\```python
POLICY = {
    "max_retry":           3,
    "on_blocked":          "skip_and_continue",
    "design_modify_scope": ["impl", "internal_interface"],
    "completion":          "all_done_or_blocked",
}
\```

## Execution Tree

\```
RootNode // root description (in-progress) @v:1.0
    TaskA // task A (done)
    TaskB // task B (in-progress) @dep:TaskA
    TaskC // task C (designing) @dep:TaskB
\```
```

### DESIGN → WORKPLAN Conversion Rules

1. Copy the Gantree tree from DESIGN as-is
2. Initialize all node statuses to `(designing)`
3. Change the top-level execution nodes with no dependencies to `(in-progress)`
4. Add POLICY block (configure to project characteristics, template: workplan-reference.md §2)
5. Do not include PPR def blocks in WORKPLAN (reference DESIGN instead)
6. Maintain `(blocked)` node status from DESIGN
7. Filename: `DESIGN-{Name}.md` → `WORKPLAN-{Name}.md` (use the same {Name})

### Lightweight WORKPLAN (standalone use without DESIGN)

For simple tasks, a WORKPLAN can be written and executed without a DESIGN.
In this case, describe the task specification directly using `#` inline comments under the node:

```text
TaskNode // task description (designing)
    # Task: what to do
    # Target: file path
    # Output: deliverable
```

The Loop engine automatically extracts `#` comments and injects them into the execution prompt.
Details: workplan-reference.md §2 Lightweight WORKPLAN

---

## 5. status-{Name}.json Structure

A JSON file that tracks execution state. Automatically created/updated during WORKPLAN execution.

```json
{
  "project": "ProjectName",
  "workplan": ".pgf/WORKPLAN-ProjectName.md",
  "design": ".pgf/DESIGN-ProjectName.md",
  "started_at": "2026-03-10T14:00:00",
  "updated_at": "2026-03-10T15:30:00",
  "iteration": 5,
  "nodes": {
    "TaskA": {
      "status": "done",
      "completed_at": "2026-03-10T14:10:00",
      "outputs": {
        "files_created": [],
        "files_modified": ["src/module_a.rs"],
        "notes": ""
      }
    },
    "TaskB": {"status": "in-progress", "started_at": "2026-03-10T14:15:00"},
    "TaskC": {"status": "designing"},
    "TaskD": {"status": "blocked", "blocker": "External API unresponsive"}
  },
  "summary": {
    "total": 4,
    "done": 1,
    "in-progress": 1,
    "designing": 1,
    "blocked": 1
  }
}
```

For Lightweight WORKPLAN, the `"design"` field is `""` (empty string).

---

## 6. Node State Transitions and Update Protocol

The authoritative source for state transition rules, automatic transition conditions, and update protocols (including PPR) is in [workplan-reference.md §3](workplan-reference.md).

Summary:

```
designing → in-progress → done
                       ↘ blocked (when blocker occurs)
```

When changing node status, **always** update both the WORKPLAN file and status JSON simultaneously.

---

## 7. REVIEW-{Name}.md Structure

Persistent artifact for `review` and `design-review` mode outputs.

```markdown
# REVIEW-{Name}

## Scope
- Target: {description of what was reviewed}
- Date: {iso8601}
- Mode: review | design-review

## Summary
{1-3 sentence overview}

## Findings

### [severity][category] {title}
- Evidence: {file:line or quote}
- Impact: {what breaks or degrades}
- Recommendation: {specific fix}

## Accepted Deferrals
- {items explicitly deferred with rationale}

## Next Actions
- {prioritized fix list}
```

`{Name}` follows the same CamelCase convention as DESIGN/WORKPLAN files.
