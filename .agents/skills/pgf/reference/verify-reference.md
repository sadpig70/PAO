# Verify Phase — Execution Result Cross-Verification Specification

## 1. Overview

The verify phase performs cross-verification of execution results from **3 perspectives** after execute completion.

### Purpose

- Eliminate blind spots of single-perspective verification
- Auto-regress reworkable issues, report design defects to user
- Support 2-perspective verification even in Lightweight WORKPLAN (no DESIGN)

### 3-Perspective Verification

| Perspective | Target | In Lightweight Mode |
|------|------|----------------|
| **Acceptance Criteria** | Do completed nodes satisfy acceptance criteria? | Use inline `# criteria:` |
| **Code Quality** | Reuse, quality, efficiency of changed code | Same (`/simplify` integration) |
| **Architecture** | Consistency between design structure and implementation structure | **Skipped** (no DESIGN) |

---

## 2. Verification Process Details

### 2.1 Acceptance Criteria Verification

Extract `acceptance_criteria` from PPR def blocks in DESIGN-{Name}.md, and AI determines whether each completed node's outputs meet the criteria.

**Standard Mode** (DESIGN exists):
- Extract `acceptance_criteria` from per-node PPR def blocks in DESIGN-{Name}.md
- Verify each completed node's outputs against criteria

**Lightweight Mode** (no DESIGN):
- Use `# criteria:` lines from `#` inline comments under nodes in WORKPLAN-{Name}.md as acceptance_criteria
- Canonical spelling is lowercase `# criteria:`. Accept case-insensitive variants (`# Criteria:`) for backward compatibility.
- For nodes without `# criteria:` lines, use the node description (`// description`) as implicit criteria

### 2.2 Code Quality Verification

Verify changed code in integration with the `/simplify` skill.

- Changed file list is extracted from outputs recorded during WORKPLAN execution (the `outputs` field in status-{Name}.json)
- Verification items: reuse (duplicate code), quality (code quality), efficiency (performance)
- Issues found by `/simplify` are collected into a `QualityReport`

### 2.3 Architecture Consistency Verification

Compare the Gantree structure from DESIGN-{Name}.md against the actual implementation code's module structure.

- Verify mapping between Gantree node hierarchy and actual directory/module structure
- Verify that `@dep:` dependencies are maintained in actual code (imports, call relationships)
- In Lightweight WORKPLAN: this verification is **skipped** (no DESIGN available)

---

## 3. Verification Result Judgment

```python
class VerifyIssue:
    node: str           # Node where issue was found
    perspective: str    # "acceptance" | "quality" | "architecture"
    severity: str       # "low" | "medium" | "high"
    description: str    # Issue description
    suggestion: str     # Fix suggestion

class VerifyResult:
    status: Literal["passed", "rework", "blocked"]
    issues: list[VerifyIssue]  # Fix targets on rework
    report: str  # Verification report
```

| Judgment | Condition | Follow-up Action |
|------|------|-----------|
| `passed` | All 3 perspectives passed | Completion report |
| `rework` | Only low/medium issues exist | Re-execute affected subtree |
| `blocked` | High issues exist (design itself needs change) | Report to user + halt |

---

## 4. Rework Rules

### Rollback Procedure

1. Identify rework target nodes (VerifyIssue's `node` field)
2. Roll back target node + child nodes to `(designing)`
3. Update WORKPLAN-{Name}.md + status-{Name}.json
4. Re-execute `execute_all_nodes()` → re-verify with `verify_project()`

### Iteration Limit

- Allow verify → rework repetition up to `POLICY.max_verify_cycles` (default: `2`)
- When limit exceeded:
  - Preserve all outputs produced so far (do not delete)
  - Halt report including unresolved issue list + attempt count
  - In create mode, return `CreationResult.verify_status = "rework_limit_exceeded"`

### Rollback Scope Determination

```python
def identify_rework_scope(issues: list[VerifyIssue], workplan_path: str) -> list[str]:
    """Identify rework target nodes + child nodes"""
    target_nodes = set(i.node for i in issues)
    descendants = set()
    for node in target_nodes:
        descendants.update(get_descendants(workplan_path, node))
    return list(target_nodes | descendants)
```

---

## 5. PPR Definition

```python
def verify_project(design_path: str, workplan_path: str, policy: dict) -> VerifyResult:
    """3-perspective cross-verification of execution results

    If design_path is empty string, Lightweight mode → verify acceptance + quality only
    """
    acceptance = verify_acceptance(design_path, workplan_path)
    quality = verify_code_quality(workplan_path)

    if design_path:
        arch = verify_architecture(design_path, workplan_path)
    else:
        arch = ArchReport(status="skipped", reason="Lightweight WORKPLAN")

    return determine_verdict(acceptance, quality, arch, policy)

def verify_acceptance(design_path: str, workplan_path: str) -> AcceptanceReport:
    """Verify completed nodes based on acceptance_criteria"""
    nodes = get_completed_nodes(workplan_path)
    results = []
    for node in nodes:
        if design_path:
            criteria = AI_extract_acceptance_criteria(design_path, node.name)
        else:
            criteria = AI_extract_inline_criteria(workplan_path, node.name)
        passed = AI_verify_against_criteria(node, criteria)
        results.append(NodeAcceptance(node=node.name, passed=passed, criteria=criteria))
    return AcceptanceReport(results=results)

def verify_code_quality(workplan_path: str) -> QualityReport:
    """Changed code quality verification — /simplify integration"""
    changed_files = AI_identify_changed_files(workplan_path)
    return AI_run_simplify_check(changed_files)

def verify_architecture(design_path: str, workplan_path: str) -> ArchReport:
    """Design vs. implementation architecture consistency"""
    design_tree = AI_parse_gantree(design_path)
    impl_structure = AI_scan_implementation(workplan_path)
    return AI_compare_design_impl(design_tree, impl_structure)

def determine_verdict(acceptance, quality, arch, policy) -> VerifyResult:
    """Aggregate judgment from 3 perspectives"""
    issues = []
    issues.extend(acceptance.failures)
    issues.extend(quality.issues)
    if arch.status != "skipped":
        issues.extend(arch.mismatches)

    if not issues:
        return VerifyResult(status="passed")

    reworkable = [i for i in issues if i.severity in ["low", "medium"]]
    blocking = [i for i in issues if i.severity == "high"]

    if blocking:
        return VerifyResult(status="blocked", issues=blocking)
    return VerifyResult(status="rework", issues=reworkable)
```

---

## 6. Verification Report Format

```text
[PGF VERIFY] === Verification Results ===
  Target: WORKPLAN-{Name}.md
  Perspective 1 — Acceptance: {passed|failed} ({n}/{total} nodes passed)
  Perspective 2 — Code Quality: {passed|issues_found} ({n} issues)
  Perspective 3 — Architecture: {passed|mismatches|skipped}

  Judgment: {passed | rework | blocked}
  {If rework: target node list}
  {If blocked: reason}
```

### Report Examples

**passed**:
```text
[PGF VERIFY] === Verification Results ===
  Target: WORKPLAN-AuthSystem.md
  Perspective 1 — Acceptance: passed (12/12 nodes passed)
  Perspective 2 — Code Quality: passed (0 issues)
  Perspective 3 — Architecture: passed

  Judgment: passed
```

**rework**:
```text
[PGF VERIFY] === Verification Results ===
  Target: WORKPLAN-AuthSystem.md
  Perspective 1 — Acceptance: failed (10/12 nodes passed)
  Perspective 2 — Code Quality: issues_found (3 issues)
  Perspective 3 — Architecture: passed

  Judgment: rework
  Target nodes: TokenValidator, SessionManager
```

**blocked**:
```text
[PGF VERIFY] === Verification Results ===
  Target: WORKPLAN-AuthSystem.md
  Perspective 1 — Acceptance: failed (8/12 nodes passed)
  Perspective 2 — Code Quality: issues_found (7 issues)
  Perspective 3 — Architecture: mismatches

  Judgment: blocked
  Reason: CoreAuth module interface design change required (DESIGN and implementation structure mismatch)
```

---

## 7. Verification Perspective Customization

Additional perspectives beyond the default 3 can be configured via the `verify_perspectives` field in POLICY.

```python
POLICY = {
    ...
    "verify_perspectives": ["performance", "security", "maintainability"],
}
```

### Additional Perspective Processing

```python
def verify_custom_perspectives(workplan_path: str, perspectives: list[str]) -> list[CustomReport]:
    """Verify additional perspectives defined in POLICY"""
    reports = []
    for perspective in perspectives:
        report = AI_verify_perspective(workplan_path, perspective)
        reports.append(report)
    return reports
```

Issues found in additional perspectives are also converted to `VerifyIssue` and included in `determine_verdict`. Severity judgment is determined by AI according to the perspective's characteristics.

| Perspective Example | Verification Content |
|-----------|-----------|
| Performance | Time/space complexity, unnecessary loops, N+1 queries |
| Security | Input validation, authentication/authorization, sensitive data exposure |
| Maintainability | Naming consistency, module cohesion, documentation level |

---

## 8. Verify Flow in Create Mode

In create mode (`/PGF create`), verify runs automatically as Phase 5. See the unified `cycle()` function in `cycle-reference.md` for detailed flow.

```
Phase 4 EXECUTE complete
    ↓
Phase 5 VERIFY
    ├─ passed → Creation complete
    ├─ rework → Roll back target nodes → Re-execute Phase 4 → Re-verify Phase 5
    │           (repeat up to max_verify_cycles)
    └─ blocked → Preserve outputs + halt report
```
