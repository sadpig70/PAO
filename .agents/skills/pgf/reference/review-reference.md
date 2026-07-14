# Review Mode — Iterative Review & Improvement Specification

> 기존 산출물(문서, 설계, 스킬, 코드)을 면밀히 검토하여 수정/개선/추가를 반복한다.
> `design --analyze`(코드→DESIGN 역공학)와 다름: review는 **이미 존재하는 산출물의 품질을 올리는** 모드.

---

## 1. Overview

### Purpose

- 기존 산출물의 불일치, 누락, 모호, 개선점을 체계적으로 도출
- 발견된 이슈를 우선순위화하여 수정·검증
- 이슈가 소진될 때까지 반복 (Convergence Loop)

### When to Use

| Situation | Example |
|-----------|---------|
| 문서 품질 개선 | PG/PGF 스킬 문서 검토 |
| 설계 검증 | DESIGN.md 내부 일관성 확인 |
| 스킬 강화 | 기존 스킬의 누락 기능 보완 |
| 코드 리뷰 | 구현 코드의 품질/보안/성능 검토 |
| 교차 검증 | 여러 문서 간 일관성 확인 |

---

## 2. Commands

| Command | Action |
|---------|--------|
| `/PGF review {target}` | 대상 파일/디렉토리 면밀 검토 |
| `/PGF review {target} --scope {files}` | 특정 파일 범위만 검토 |
| `/PGF review {target} --max-cycles N` | 최대 N회 반복 (기본: 이슈 소진까지) |

---

## 3. Execution Flow

```python
def review_cycle(
    target: str,
    scope: list[str] = None,
    max_cycles: int = 10,
) -> ReviewResult:
    """면밀 검토 → 수정 → 재검증 반복"""

    cycle = 0
    all_fixes = []

    while cycle < max_cycles:
        cycle += 1

        # Phase 1: ANALYZE — 다각도 분석
        issues = analyze(target, scope)

        if not issues:
            break  # 이슈 소진 → 완료

        # Phase 2: PRIORITIZE — 우선순위 결정
        prioritized = prioritize_issues(issues)

        # Phase 3: IMPLEMENT — 수정 구현
        fixes = implement_fixes(prioritized)
        all_fixes.extend(fixes)

        # Phase 4: VERIFY — 수정 검증
        remaining = verify_fixes(target, fixes)

        report_cycle(cycle, len(issues), len(fixes), len(remaining))

        if not remaining:
            break  # 모든 이슈 해결

    return ReviewResult(
        cycles=cycle,
        total_issues=len(all_fixes),
        status="passed" if cycle < max_cycles else "max_cycles_reached",
    )
```

---

## 4. Analysis Framework

```python
def analyze(target: str, scope: list[str]) -> list[Issue]:
    """5축 분석"""
    content = read_all(target, scope)

    [parallel]
        consistency = AI_check_internal_consistency(content)
        # 같은 문서 안에서 모순되는 설명

        completeness = AI_check_completeness(content)
        # 핵심 개념이 누락 없이 정의되었는가

        clarity = AI_check_clarity(content)
        # 모호한 표현, 해석이 갈릴 수 있는 부분

        accuracy = AI_check_accuracy(content)
        # 예시가 설명과 일치하는가, 참조가 유효한가

        improvements = AI_identify_improvements(content)
        # 더 나은 표현, 추가할 개념, 구조 개선

    # 다중 파일 대상 시 교차 일관성 추가
    if len(scope or [target]) > 1:
        cross = AI_check_cross_consistency(content)
        return merge_deduplicate(consistency, completeness, clarity, accuracy, improvements, cross)

    return merge_deduplicate(consistency, completeness, clarity, accuracy, improvements)
```

### Issue Format

```python
Issue = {
    "id": str,           # P1, F2, C3 등
    "location": str,     # 파일:섹션 또는 파일:라인
    "type": str,         # "fix" | "improve" | "add"
    "impact": str,       # "high" | "medium" | "low"
    "description": str,  # 이슈 설명
    "suggestion": str,   # 제안된 수정
}
```

---

## 5. Prioritization

```python
def prioritize_issues(issues: list[Issue]) -> list[Issue]:
    """impact × type 기준 정렬"""
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

---

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

---

## 7. Relationship with Other Modes

| Mode | Relationship |
|------|-------------|
| `design --analyze` | 코드→DESIGN 역공학. review는 기존 산출물 품질 개선 |
| `verify` | 구현 후 검증. review는 구현 전/후 불문 산출물 검토 |
| `design-review` (3관점) | DESIGN→PLAN 전환 전 사전 검증. review는 범용 반복 검토 |
