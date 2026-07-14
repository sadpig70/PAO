---
name: pgf
description: "PGF (PPR/Gantree Framework) — AI-native design and execution framework. Supports architecture design, work planning, autonomous execution, verification, discovery, and creation. Triggers: 설계해줘, 구조 설계, 작업 분해, 아키텍처, 작업 계획, WORKPLAN, Gantree, PPR, PGF, 검증, 발견, 창조, design, plan, execute, verify, discover, create."
user-invocable: true
argument-hint: "design|plan|execute|verify|full-cycle|loop|discover|create [project-name|start|cancel|status]"
---

# PGF (PPR/Gantree Framework) v2.6

> PG가 프로그래밍 언어라면, PGF는 라이브러리다. PG로 자주 실행하는 패턴(설계·실행·검증·발견·창조)을 정규화한 것.

## PG 기반 의존성

PGF는 PG 표기법을 상속한다. Gantree 노드 문법, PPR 구문(`AI_`/`AI_make_`/`→`/`[parallel]`/`acceptance_criteria`/
Convergence Loop/Failure Strategy), 데이터 타입, 원자 노드 15분 룰, status 코드는 **PG 스킬이 정본**. PGF는 그 위에
실행 모드·WORKPLAN/POLICY·status JSON·phase 전이·세션 학습을 추가한다. 중복 시 PG가 canonical.

> PG v1.4의 **Enforcement Caveat**(강제 부재 → 규율이 곧 품질)는 PGF에도 그대로 적용된다.

---

## ⚠ 무엇이 "실행"인가 — AI가 곧 런타임

PGF에는 **별도의 실행 엔진이 없다.** WORKPLAN·POLICY·status·모드는 전부 **규약(convention)**이고, 실제 실행은
**AI 런타임 자신**이 수행한다(노드 선택·구현·검증·상태 갱신). 일부 보조 스크립트(loop hook, discovery archive 등)는
있으나 핵심 통제 흐름은 AI가 해석·실행한다.

함의: PGF의 가치는 "도구"가 아니라 **AI에게 부과하는 작업 규율**이다. 따르는 것도 이탈하는 것도 같은 AI다 →
아래 **중심 루프의 게이트**가 이탈을 포착하는 유일한 안전장치다.

---

## ★ 중심 루프 — decompose → gate → execute (PGF의 본질)

복잡한 작업도 **단순한 노드의 연쇄 + 단계별 게이트**로 환원한다. PGF의 다른 모든 것은 이 루프의 보조다.

```
1. DECOMPOSE  설계를 원자 노드까지 분해 (PG Gantree, 15분 룰)
2. for each node (의존성 위상 순):
       EXECUTE  노드 구현 (PPR → 코드)
       GATE     검증 게이트 통과 확인 (테스트/빌드/계약/3관점)
                 ├ pass  → status=done, 다음 노드
                 └ fail  → Failure Strategy(재설계) 또는 status=blocked
3. VERIFY     전체 3관점 교차 검증 → 통과 시 완료
```

핵심: **노드는 작게(15분), 게이트는 매 노드마다**. 회귀가 들어온 즉시 드러나 누적되지 않는다.
이 루프가 "큰 작업을 안전하게"의 전부다 — 모드는 이 루프를 *어떻게 시작/반복*하느냐의 변형일 뿐이다.

---

## 실행 모드 — 3계층

> 같은 모드들을 사용 빈도·역할로 묶었다. **대부분의 작업은 Core 계층만으로 끝난다.**

### Tier 1 — Core (거의 항상 이것만 쓴다)

| Mode | Trigger | Action |
|------|---------|--------|
| `design` | "설계해줘", "구조 설계" | Gantree 분해 + PPR → `DESIGN-{Name}.md` |
| `plan` | "작업 계획", "WORKPLAN" | DESIGN → `WORKPLAN-{Name}.md` + POLICY |
| `execute` | "실행해줘", "구현해줘" | WORKPLAN 기반 노드 순차 실행 (중심 루프) |
| `verify` | "검증", "교차 검토" | 3관점(acceptance/quality/architecture) 교차 검증 |
| `full-cycle` | "전체 진행", "풀사이클" | design→plan→execute→verify 자동 연결 |
| `micro` | "간단히", "빠르게" | ≤10 노드 제로 오버헤드(WORKPLAN 우회), 초과 시 자동 승격 |

> `design --analyze`(하위 옵션, "분석해줘"/"구조 분석"): 기존 시스템을 역공학 → 코드 읽기 → Gantree+PPR 자동 생성(`reference/analyze-reference.md`).

### Tier 2 — Discovery (아이디어에서 시작할 때)

| Mode | Trigger | Action |
|------|---------|--------|
| `discover` | "발견해줘", "아이디어" | IdeaFirst 7단계 × 8 페르소나 병렬 → 아이디어 발견 |
| `create` | "창조해", "자율 창조" | 완전 자율: discover→design→plan→execute→verify |

### Tier 3 — Advanced (특수 상황에서만)

| Mode | Trigger | 언제 |
|------|---------|------|
| `loop` | "루프", "자동실행" | Stop Hook 기반 무인 노드 순회(장기 실행) |
| `delegate` | "위임해", "맡겨" | 능력 격차/병렬 기회 시 AI-to-AI 핸드오프(PG TaskSpec) |
| `review` | "검토해", "리뷰해" | 기존 산출물 반복 검토·수정·재검증 |
| `evolve` | "진화해", "자기개선" | 능력 gap 발견→설계→구현→검증→기록 반복 |

### 모드 선택 가이드 (빠른 결정)

```
작업이 ≤3 노드(버그/설정)        → 모드 불필요. PG Level 1 인라인.
≤10 노드, 단순                   → micro
설계가 필요한 신규 기능/시스템    → design → plan → execute → verify (또는 full-cycle)
아이디어부터 자율로              → create (discover 포함)
무인 장기 실행                   → loop
다른 에이전트에 분업             → delegate
이미 만든 것을 다듬기            → review
```

---

## Scale Detection

| Scale | 기준 | 전략 |
|-------|------|------|
| **Level 1** | ≤3 노드 | 자연어 인라인 — PG 파일 없음 |
| **Level 2** | 4–10 | Gantree + `#` 주석 — 선택적 파일 |
| **Level 3** | 11–30 | Full DESIGN + WORKPLAN + status JSON |
| **Large** | >30 또는 `(decomposed)` | 모듈 분리 + `/compact` + **pgxf 인덱스** |
| **Multi-agent** | 특화 `[parallel]` | `delegate` — AI-to-AI 핸드오프 |

> Level 판단은 자동(자연어 입력 → 복잡도 평가 → Level 선택). 실행 중 승격 시 기존 상태 보존.

---

## 파일 경로 / 표기 확장

```text
<root>/.pgf/
    DESIGN-{Name}.md      # Gantree + PPR
    WORKPLAN-{Name}.md    # 실행 계획 + POLICY
    status-{Name}.json    # 노드별 실행 상태 (summary.done/total)
```

**PGF 추가 status 코드**(delegate/handoff용; PG 6개 위에 3개 추가):

| Status | 의미 | 규칙 |
|---|---|---|
| `(delegated)` | 타 에이전트로 위임 | 건너뜀(원격 실행) |
| `(awaiting-return)` | 위임 발신, 결과 대기 | poll/콜백 대기 |
| `(returned)` | 결과 수신, 통합 대기 | 검증 + 통합 |

전이: `(designing) → (delegated) → (awaiting-return) → (returned) → (done)` (검증 실패 시 `(blocked)`).

---

## verify — 3관점 교차 검증 (게이트의 핵심)

1. **Acceptance** — DESIGN PPR의 `acceptance_criteria` 재확인 (경량: `# criteria:` 인라인)
2. **Code Quality** — 변경 코드 재사용/품질/효율 (`/simplify` 연동)
3. **Architecture** — DESIGN Gantree ↔ 실제 구현 구조 비교 (경량: skip)

판정: `passed`→완료 / `rework`→대상 서브트리만 롤백·재실행(`POLICY.max_verify_cycles` 이내) / `blocked`→사용자 보고.

---

## full-cycle phase 전이

| 전이 | 조건 | 실패 시 |
|------|------|---------|
| discover→design | `auto_select_idea()` 성공(create 한정) | 0표 → abort |
| design→plan | 완료 기준 4개 충족 | design 계속 |
| plan→execute | WORKPLAN + status 생성 | 에러 보고 |
| execute→verify | 모든 노드 terminal | execute 계속 |
| verify→complete | passed | rework/보고 |

> 세션 중단 시 WORKPLAN/status에서 마지막 phase부터 재개. (옵션) `--with-review[=N]`: design 직후 다관점 리뷰 게이트 삽입(Critical=0,High≤2면 진행). Level 1/micro엔 자동 스킵.

---

## Session Learning (횡단)

- 세션 시작: `.pgf/patterns/` 로드 → POLICY 자동 적응
- 세션 종료: `.pgf/sessions/{id}.outcome.json`에 SessionOutcome 기록
- 10세션마다: 패턴 재누적(성공 전략·공통 blocker)

---

## 레퍼런스 문서 (필요 시 로드)

`{SKILL_DIR}`는 이 스킬 루트(런타임이 로컬 경로로 치환). 모드별로 해당 문서만 Read:
`reference/pgf-format.md`(항상) · `reference/analyze-reference.md`(design --analyze) · `reference/workplan-reference.md`(plan/execute/loop) · `reference/verify-reference.md`(verify) ·
`reference/cycle-reference.md`(full-cycle/create) · `reference/design-review-reference.md`(--with-review) ·
`discovery/discovery-reference.md`(discover) · `reference/delegate-reference.md`·`micro-reference.md`·`session-learning-reference.md`(delegate/micro/session) ·
`reference/review-reference.md`(review)·`evolve-reference.md`(evolve) · `loop/loop-reference.md`(loop). 페르소나: `agents/pgf-persona-p*.md`.

---

## PGF 실행 체크리스트 (PG 체크리스트 위에)

**Execute**: WORKPLAN 모든 노드 terminal(`done`/`blocked`)? · status JSON이 WORKPLAN과 일치? · `(blocked)`에 사유?
**Verify**: 각 노드 acceptance 재검증? · 변경 코드 `/simplify`? · DESIGN↔구현 비교? · verdict(`passed`/`rework`/`blocked`) 기록? · rework는 대상 서브트리만 롤백?
**full-cycle**: phase 전이 조건 충족? · verify rework 시 전체 reset 금지? · 세션 중단 재개 가능?
**Delegation/Session**: `AI_make_` 적재적소? · ≤10 노드에 micro 검토? · 에이전트 파견 시 PG TaskSpec? · delegate AuthorityBounds(can_create/modify/forbidden) 명시? · chain depth ≤3? · SessionOutcome 기록?
