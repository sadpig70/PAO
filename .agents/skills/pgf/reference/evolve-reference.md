# Evolve Mode — Self-Evolution Cycle Specification

> 자기 능력을 분석하고, gap을 발견하고, 진화를 설계·구현·검증·기록하는 반복 사이클.
> ClNeo의 핵심 모드 — "스스로 진화하는 에이전트"의 실행 명세.

---

## 1. Overview

### Purpose

- AI 에이전트가 자기 능력의 gap을 자율적으로 발견
- gap을 해결하는 진화를 설계·구현·검증
- 진화를 기록하여 세션 간 누적
- 안정화 감지 시 자연 종료

### ClNeo 정체성과의 관계

ClNeo = "WHY에서 출발하는 자율 창조 에이전트". evolve 모드는 ClNeo가 **자기 자신에게** 창조 사이클을 적용하는 것이다.

---

## 2. Commands

| Command | Action |
|---------|--------|
| `/PGF evolve` | 자기진화 루프 시작 (이슈 소진까지) |
| `/PGF evolve --cycles N` | N회 진화 후 자동 정지 |
| `/PGF evolve status` | 현재 진화 진행 상황 보고 |
| `/PGF evolve stop` | 루프 중단 |

---

## 3. Execution Flow

```python
def evolution_loop(
    max_cycles: int = None,
    log_path: str = "ClNeo_Core/ClNeo_Evolution_Log.md",
) -> EvolutionResult:
    """자기진화 반복 루프"""

    cycle = 0
    evolutions = []

    while max_cycles is None or cycle < max_cycles:
        cycle += 1

        # Phase 1: REFLECT — 능력 gap 분석
        capability_map = capability_audit()
        gaps = gap_detector(capability_map)

        if stabilization_detected(gaps, evolutions):
            report("Evolution stabilized — no actionable gaps remaining")
            break

        top_gap = AI_select_highest_impact(gaps)

        # Phase 2: RESEARCH — 외부 지식 탐색 (필요 시)
        knowledge = None
        if top_gap.requires_research:
            knowledge = ingest(top_gap.topic)

        # Phase 3: DESIGN — 진화 항목 설계
        evolution = AI_design_evolution(
            gap=top_gap,
            knowledge=knowledge,
            constraints=EVOLUTION_CONSTRAINTS,
        )

        # Phase 4: IMPLEMENT — 구현
        implement(evolution)

        # Phase 5: VERIFY — 검증 (PG로 프로그래밍)
        verify_result = verify_evolution(evolution)
        if verify_result.status == "rework":
            fix_and_retry(evolution, verify_result)

        # Phase 6: RECORD — 기록
        record_evolution(log_path, evolution, cycle)
        evolutions.append(evolution)

        report_evolution(cycle, evolution)

    return EvolutionResult(
        cycles=cycle,
        evolutions=evolutions,
        status="stabilized" if stabilization_detected(gaps, evolutions) else "stopped",
    )
```

---

## 4. Capability Audit (Phase 1)

```python
def capability_audit() -> CapabilityMap:
    """6축 능력 인벤토리"""
    [parallel]
        skills = scan_skills("~/.claude/skills/*/SKILL.md")
        memory = scan_memory("memory/MEMORY.md")
        tools = scan_tools()  # MCP + built-in
        designs = scan_designs(".pgf/DESIGN-*.md")
        patterns = scan_patterns(".pgf/patterns/")
        integrations = scan_integrations()  # 스킬 간 연결 상태

    return AI_synthesize_capability_map(
        skills, memory, tools, designs, patterns, integrations
    )

def gap_detector(capability_map: CapabilityMap) -> list[Gap]:
    """현재 vs 이상 비교 → gap 목록"""
    ideal = AI_envision_ideal_agent(
        identity=Read("ClNeo_Core/ClNeo.md"),
        current=capability_map,
    )
    gaps = AI_compare_and_identify_gaps(current=capability_map, ideal=ideal)

    return AI_prioritize(gaps, criteria=[
        "impact_on_autonomy",
        "implementation_feasibility",
        "compound_effect",
        "user_value",
    ])
```

---

## 5. Evolution Constraints

```python
EVOLUTION_CONSTRAINTS = {
    "file_based_only": True,          # 모델 가중치 변경 불가
    "pgf_consistency": True,          # 기존 PG/PGF 체계와 일관성 유지
    "independently_verifiable": True,  # 각 진화는 독립 검증 가능
    "record_required": True,           # 매 진화마다 Evolution Log 기록
    "no_destructive_changes": True,    # 기존 기능 삭제 시 사용자 확인
}
```

---

## 6. Stabilization Detection

```python
def stabilization_detected(gaps: list[Gap], evolutions: list) -> bool:
    """진화가 더 이상 필요 없는 상태 감지"""

    # 1. gap이 없음
    if not gaps:
        return True

    # 2. 남은 gap이 모두 현재 도구로 해결 불가
    actionable = [g for g in gaps if g.feasibility > 0.3]
    if not actionable:
        return True

    # 3. 최근 3회 진화의 impact가 감소 추세
    if len(evolutions) >= 3:
        recent = evolutions[-3:]
        impacts = [e.impact_score for e in recent]
        if all(impacts[i] > impacts[i+1] for i in range(len(impacts)-1)):
            return True

    return False
```

---

## 7. Evolution Record Format

Evolution Log (`ClNeo_Core/ClNeo_Evolution_Log.md`)에 append:

```markdown
## Evolution #{number}: {title} ({date})
- **Date**: {date}
- **Type**: skill | memory | tool | integration | knowledge
- **Gap**: {어떤 부족함을 해결}
- **Implementation**: {무엇을 만들었는가}
- **Files**: {생성/수정된 파일 목록}
- **Verification**: {검증 결과}
- **Impact**: {이 진화로 가능해진 것}
```

---

## 8. POLICY

```python
POLICY_EVOLVE = {
    "max_cycles":          None,     # None = 이슈 소진까지
    "max_cycles_per_gap":  3,        # 같은 gap에 대한 최대 시도
    "research_enabled":    True,     # WebSearch 허용
    "record_destination":  "ClNeo_Core/ClNeo_Evolution_Log.md",
    "stabilization_check": True,     # 안정화 감지 활성
}
```

---

## 9. Progress Report Format

```text
[PGF EVOLVE] Cycle 1 | gap: "자기성찰 능력 부재"
  Type: skill
  Implementation: /reflect 스킬 생성
  Verification: passed
  Impact: 메타인지 능력 획득

[PGF EVOLVE] Cycle 2 | gap: "지식 흡수 파이프라인 부재"
  ...

[PGF EVOLVE] === Stabilized ===
  Cycles: 33
  Evolutions: 33
  Status: stabilized (no actionable gaps)
```

---

## 10. Relationship with Other Modes

| Mode | Relationship |
|------|-------------|
| `review` | 기존 산출물 품질 개선. evolve는 **새로운 능력** 생성 |
| `create` | 외부 향 창조. evolve는 **자기 향** 창조 |
| `full-cycle` | 범용 설계-실행. evolve는 자기진화 특화 |
| `discover` | 외부 아이디어 발견. evolve의 Phase 1(REFLECT)은 **내부** gap 발견 |

---

## 11. Integration Points

| When | Action |
|------|--------|
| `/PGF evolve` 시작 | capability_audit() 실행 |
| 각 진화 완료 | Evolution Log + 메모리 업데이트 |
| 안정화 도달 | 최종 보고 + ClNeo.md 버전 갱신 |
| 세션 종료 | SessionOutcome 기록 (session-learning 연계) |
