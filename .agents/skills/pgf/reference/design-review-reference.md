# Design Review Protocol

## Purpose

DESIGN 완료 → PLAN 전환 전에 설계 품질을 다관점으로 검증.
구현 후 rework보다 설계 단계에서 문제를 잡는 것이 10배 저렴하다.

## When to Trigger

- `design` 모드의 4개 완료 기준이 충족된 후 (사용자 명시 호출)
- `full-cycle` / `create` 모드에서 **`--with-review[=N]` 플래그가 있을 때만** design → plan 전환 직전 (opt-in, 기본 비활성)
- 사용자가 `/PGF design-review` 또는 `/PGF review --design` 요청 시

> 기본 풀사이클은 안정성을 위해 review 게이트를 포함하지 않는다. 검토가 필요한 작업에서만 명시적으로 플래그를 부여하라.

## Loop Contract (full-cycle `--with-review[=N]` 호출 시)

`full_cycle()`이 호출하는 진입점은 `run_design_review_loop(design_path, max_iterations, status_path)` 이며 다음을 보장한다:

1. **반복 상한**: 최대 N회 (`max_iterations`) review-revise 사이클. N의 기본값은 1
2. **통과 기준**: `Critical=0 AND High≤2` → `status="passed"` 즉시 반환
3. **revise 동작**: 통과 실패 시 `AI_revise_design(design_path, issues)` 호출 후 재검토
4. **N회 초과 시**: `status="needs_user_ack"` 반환 + `unresolved_issues` 동봉 (강제 차단 아님)
5. **status JSON 갱신**: `review_iterations`(int), `unresolved_issues`(list[Issue])만 추가 — 기존 키 불변
6. **Skip 자동화**: Level 1 (≤3 노드) / `micro` 모드 → 호출 자체를 스킵

## 3-Perspective Design Review

기존 8 페르소나 중 3개 관점을 선택하여 경량 리뷰:

| Reviewer | Persona Base | Focus |
|----------|-------------|-------|
| **Feasibility Reviewer** | P5 (Field Operator) | 구현 가능성, 기술 선택, 복잡도 |
| **Risk Reviewer** | P7 (Contrarian Critic) | 치명적 약점, 숨은 가정, 확장성 위험 |
| **Architecture Reviewer** | P8 (Convergence Architect) | 구조 일관성, 모듈 결합도, 진화 가능성 |

## Review Process

```
def design_review(design_path: str) -> ReviewResult:
    design = Read(design_path)

    [parallel]
        feasibility = Agent(
            persona = "P5 Field Operator",
            prompt = f"""
            Review this PGF DESIGN for implementation feasibility:
            {design}

            Check:
            1. Can every node be implemented with available tools?
            2. Are there hidden dependencies not captured in @dep?
            3. Is the complexity estimate realistic (15-min atomic rule)?
            4. Are there missing error handling paths?

            Output: PASS / CONCERN (with specific issues)
            """
        )

        risk = Agent(
            persona = "P7 Contrarian Critic",
            prompt = f"""
            Challenge this PGF DESIGN — find its weakest points:
            {design}

            Attack from:
            1. What assumptions will break first?
            2. What's the single point of failure?
            3. What happens at 10x scale?
            4. What's missing that the designer didn't think of?

            Output: PASS / CONCERN (with specific risks)
            """
        )

        architecture = Agent(
            persona = "P8 Convergence Architect",
            prompt = f"""
            Review this PGF DESIGN for architectural quality:
            {design}

            Evaluate:
            1. Gantree hierarchy — proper decomposition?
            2. PPR def blocks — AI_ functions well-defined?
            3. Module boundaries — clean interfaces?
            4. Future extensibility — can this evolve?

            Output: PASS / CONCERN (with specific improvements)
            """
        )

    # Aggregate results
    if all_pass([feasibility, risk, architecture]):
        return ReviewResult(status="APPROVED", notes=aggregate_notes)
    else:
        concerns = collect_concerns([feasibility, risk, architecture])
        return ReviewResult(status="REVISE", concerns=concerns)
```

## Result Actions

| Result | Action |
|--------|--------|
| 3/3 PASS | Proceed to PLAN |
| 2/3 PASS | Address concerns, proceed if non-critical |
| 1/3 or 0/3 PASS | Revise DESIGN before proceeding |

## Integration with PGF Modes

- **design** → design-review → plan (manual trigger: `/PGF design-review`)
- **full-cycle** → auto-trigger after design completion criteria met
- **create** → auto-trigger (autonomous mode)

## Lightweight Mode

For Level 1-2 tasks (≤10 nodes), skip multi-agent review. Single-perspective self-review sufficient:
- Read the design
- Ask: "Would P7 (Contrarian Critic) find a fatal flaw?"
- If yes → address it. If no → proceed.
