---
name: pg
description: "PG (PPR/Gantree) — AI-native intent specification notation. Gantree for hierarchical structure decomposition, PPR for detailed logic with AI_ cognitive functions, → pipelines, and [parallel] blocks. Auto-load when encountering Gantree trees, PPR def blocks, AI_ prefixed functions, → pipelines, or PG-written documents."
user-invocable: false
disable-model-invocation: false
---

# PG — PPR/Gantree Notation v1.4

> **AI를 런타임으로 하는 DSL.** 결정론적 로직은 Python으로, AI 인지 연산은 `AI_` 접두사로 표기한다.
> 둘이 합쳐져 하나의 프로그램 — AI가 읽고(파싱이 아니라 **이해**하고) 전체 작업을 수행한다.

## Quick Start

1. **Gantree**로 작업을 계층 분해한다 (4-space 들여쓰기 = 계층)
2. 복잡한 노드만 **PPR `def`** 블록으로 상세 로직을 기술한다
3. AI 판단이 필요한 곳은 **`AI_`** 접두사, 정확한 계산은 실제 코드로 쓴다
4. 완료 조건은 **`acceptance_criteria`**로 내장한다
5. 실행 → 검증 → 필요 시 재작업

```
MyTask // 작업 설명 (in-progress)
    StepA // 첫 단계 (done)
    StepB // 두번째 단계 (in-progress) @dep:StepA
        # input: data from StepA
        # process: AI_analyze(data) → result
        # criteria: accuracy >= 0.9
```

이것이 PG의 전부다. 아래는 상세 정의.

---

## 핵심 속성 (load-bearing)

- **Parser-Free**: PG는 AI가 이미 아는 표기(Python 문법·들여쓰기 계층·함수 합성)로 구성 → 파서/컴파일러/툴체인 불필요. AI는 **파싱하지 않고 이해(comprehend)**한다. 하나의 문서가 설계 명세·구현 의도·실행 명령·소통 매체·조직 계약을 동시 수행.
- **Co-evolutionary**: AI 모델이 좋아지면 PG 문서는 **수정 없이** 더 나은 결과를 낸다. 역으로 명세 정교화가 같은 AI의 실행 정확도를 높인다. PG는 자기 자신을 분석·설계·검증할 수 있다(자기참조).
- **DL/OCME**: AI 인지 런타임을 실행 대상으로 전제하는 첫 표기. `AI_` 함수의 비결정론적 출력은 버그가 아니라 **설계 자산**.
- **AI-to-AI 기본 소통층**: 의도·구조·절차·상태·검증을 PG 구문(`AI_`·`→`·`@dep:`·`[parallel]`·`acceptance_criteria`)이 직접 전달. 자연어는 보조 메타데이터(`//`·`"""..."""`). 크로스모델 호환(Claude/Kimi/GPT/Gemini 실증).

---

## ⚠ Enforcement Caveat (반드시 읽을 것)

**PG는 강제되지 않는다(comprehension-based, not enforced).** 파서가 없다는 강점의 이면이다:
PG를 따를 수 있는 같은 AI가 **조용히 이탈**할 수도 있다(상태 코드를 안 갱신, acceptance_criteria를 건너뜀, 결정론 로직에 `AI_`를 남용). 품질은 표기가 아니라 **런타임의 규율**에 달려 있다.

대응 (자기-규율):
- 각 노드 실행 후 **status를 즉시 갱신**한다(`done`/`blocked`/`needs-verify`).
- `acceptance_criteria`는 장식이 아니다 — 실행 후 **실제로 평가**한다.
- 정확성이 필요한 곳에 `AI_`를 쓰지 않았는지 매 PPR마다 자문한다.
- 가능하면 외부 게이트(테스트·검증 스크립트·다관점 리뷰)로 이탈을 **포착**한다 — 표기 자체는 포착하지 못한다.

> 이 caveat는 PG의 약점이 아니라 **사용 조건**이다. 강제가 없으므로 규율이 곧 품질이다.

---

## Gantree — 계층 구조

들여쓰기 기반 트리로 시스템을 분해한다.

### 노드 문법

```
NodeName // description (status) [@v:version] [@dep:dependency] [#tag]
```

- **NodeName**: CamelCase 식별자 · **// description**: 자연어 설명 · **(status)**: 아래 표
- **@v:X.Y**: 버전(루트) · **@dep:A,B**: A,B 완료 후 실행 · **#tag**: 분류(검색/필터)
- **[parallel]...[/parallel]**: 병렬 실행 구간

### 상태 코드 + 실행 규칙

| Status | 의미 | AI 실행 규칙 |
|--------|------|-------------|
| `(done)` | 완료 | 건너뜀 |
| `(in-progress)` | **명세 완결 + 실행 중** | PPR def 블록 실행 |
| `(designing)` | **명세 미완결(실행 불가)** | 스텁/기본 로직만 |
| `(blocked)` | 막힘 | 건너뜀(사유 기록) |
| `(decomposed)` | 5레벨 초과로 분리 | 분리된 트리 참조 |
| `(needs-verify)` | 실행됨, 검증 대기 | 검증 → 통과 `(done)` / 재작업 `(designing)` / 복구불가 `(blocked)` |

#### ★ `designing` ↔ `in-progress` 판단 규칙

경계가 모호할 때의 결정 규칙 — **"지금 이 노드의 PPR만 보고 실행을 시작할 수 있는가?"**

- **예 → `in-progress`**: 입출력·로직이 충분히 명세되어 *바로 코드로 옮길 수 있다*. (acceptance_criteria가 있으면 거의 항상 여기.)
- **아니오 → `designing`**: 아직 무엇을/어떻게가 안 정해져 스텁밖에 못 쓴다. 하위 분해나 명세 보강이 먼저다.
- 한 문장 테스트: *"이 노드를 다른 런타임에 넘겨도 추가 질문 없이 구현하겠는가?"* → 그렇다면 `in-progress`.

### 구조 규칙

- 4 spaces = 1 level (탭 금지) · 최대 깊이 5레벨; 6레벨 진입 시 `(decomposed)`로 분리
- 자식 10+ → 중간 그룹 노드로 분기 · `[parallel]` 중첩 금지(flat만) · `[parallel]` 내부 `@dep:` 금지(병렬=독립)

### `(decomposed)` 분리

깊이 6레벨 진입 시 별도 트리로 분리하고 원본에서 참조한다:

```
OrderSystem // 주문 시스템 (in-progress)
    PaymentFlow // 결제 흐름 — see PaymentFlow tree (decomposed)
    ShippingFlow // 배송 흐름 (designing)

PaymentFlow // 분리된 결제 상세 트리 (in-progress)
    ValidateCard // 카드 검증 (done)
    ChargeCard // 카드 청구 (in-progress) @dep:ValidateCard
```

### 예시 (parallel 포함)

```
PaymentSystem // 결제 시스템 (in-progress) @v:1.0
    UserDB // 사용자 DB 연결 (done)
    Auth // 인증 (done) @dep:UserDB
    [parallel]
    ValidateCard // 카드 검증 (done)
    CheckBalance // 잔액 확인 (done)
    [/parallel]
    ProcessPayment // 결제 처리 (designing) @dep:ValidateCard,CheckBalance
```

### 원자 노드 (Atomic Node) — 15분 룰

진단 휴리스틱(7개 중 5+ 만족 시 원자화 후보): ① 입출력 명확(함수 시그니처화) ② 단일 책임("AND" 없이 한 문장)
③ 단일 함수로 완결(≈50줄 이하) ④ AI가 15분 내 완전 작성 가능 ⑤ 재분해 무의미 ⑥ 외부 의존 ≤2 ⑦ 도메인 독립.

> **최종 결정권은 15분 룰**: 휴리스틱 5개를 만족해도 15분 내 완성 불가 → 분해. 4개만 만족해도 15분 내 가능 → 원자.

---

## PPR — 상세 로직

AI가 이해하는 의도 명세. Python 문법 기반으로 인지 연산을 표기한다.

### 데이터 타입 (의도 전달용 완화 표기 허용)

```python
text: str                                          # 기본
user: dict = {"name": str, "age": int}             # 스키마 리터럴(PG 허용)
status: Literal["draft", "review", "published"]    # 열거형
nickname: Optional[str]                            # 옵셔널
Section = dict[str, str | list[str] | int]         # 타입 별칭
```

### Python과 다른 점 (5가지)

`AI_` 접두사(인지 연산 선언) · `→`(데이터 파이프라인) · `[parallel]`(병렬 구간) · 완화된 타입(의도 전달용) · import 생략.

### AI_ 함수

```python
def AI_[verb]_[target](params: Type) -> ReturnType:
    """의도 설명"""
```

4가지 인지 범주(참조용): **판단**(`AI_assess_quality`) · **추론**(`AI_generate_plan`) · **인식**(`AI_understand_intent`) · **창조**(`AI_generate_content`). snake_case, 반환 타입 명시.

**규칙(중요)**: 정밀 계산은 실제 코드, AI 판단이 필요한 곳만 `AI_`.

```python
result = AI_calculate(2 + 2)        # ❌ 정확성 필요 → 실제 코드로
result = 2 + 2                      # ✅
analysis = AI_analyze_trend(sales)  # ✅ 판단 필요 → AI_
```

> 사역(대상을 ~하게 만든다) 표현이 필요하면 `AI_make_`를 쓴다 — **부록 A** 참조(드물게 사용).

### → 파이프라인

```python
raw → AI_clean → AI_extract → AI_classify → result        # 좌 출력이 우 입력

input → { "sentiment": AI_analyze_sentiment → score,       # 분기
          "keywords": AI_extract_keywords → words }

[parallel]                                                 # 병렬 후 병합
tech = AI_analyze(data, lens="tech")
market = AI_analyze(data, lens="market")
[/parallel]
synthesis = AI_synthesize(tech, market) → result
```

**에러 전파**: 단계 실패(None/예외) 시 파이프라인 중단 + 마지막 성공 출력 반환. 무시하려면 `try/except`로 명시.

### Convergence Loop (자기 개선 반복)

```python
draft = AI_generate(brief)
while True:
    eval = AI_evaluate(draft, criteria)
    if eval.score >= threshold: break
    draft = AI_revise(draft, eval.feedback)
```

### Failure Strategy (실패 시 자기 수정)

```python
for attempt in range(max_retry):
    result = AI_execute(task)
    if AI_verify(result, acceptance_criteria): return result
    if attempt >= 1:
        task.ppr = AI_redesign(task, result.failure_reason, constraint='preserve_public_interface')
task.status = "blocked"
```

### acceptance_criteria — 검증 기준 내장 (장식 아님 — 실제로 평가할 것)

```python
def some_task(input: InputType) -> OutputType:
    """작업 설명"""
    # acceptance_criteria:
    #   - 모든 필드 포함            (기능적)
    #   - AI_assess_quality >= 0.85 (정성적)
    #   - 포맷 준수                 (구조적)
```

### 흐름 제어

파이썬 문법 그대로 (`if/else`, `try/except`).

---

## Gantree ↔ PPR 연결

| 노드 유형 | PPR 연결 | 규모 |
|----------|---------|------|
| 단순 원자 | 인라인 `AI_extract_keywords` 직접 기재 | 단일 호출 |
| 간략 PPR | 노드 아래 `#` 주석 3–7줄(`# input/process/output/criteria`) | 소규모 |
| 별도 def | 완전한 PPR 함수 정의 | 중규모+ |

---

## Progressive Formalization — 3-Level

| Level | 형식 | 적합 | 파일 |
|---|---|---|---|
| **1** | 자연어 한 줄 | 버그수정·설정(≤3 노드) | 없음(인라인) |
| **2** | Gantree + `#` 주석 | 기능추가·리팩터(4–10) | 선택 |
| **3** | Gantree + PPR `def` + acceptance | 시스템 설계·대규모(10+) | 필수 |

**자동 승격**: 실행 중 복잡도 증가 시 상위 레벨로(기존 완료 상태 보존). 처음부터 PG 문법을 알 필요 없음.

---

## PG 문서를 만나면

1. Gantree → 계층·실행 순서 2. `(status)` → 실행/건너뜀 3. `@dep:` → 순서 4. `[parallel]` → 병렬
5. PPR `def` → 해석·실행 6. `#` 간략 PPR → 인라인 해석 7. `AI_` 인라인 → 직접 실행 8. 없으면 자식으로 재귀

---

## 체크리스트

**Gantree**: 5레벨 이내? · status 명시? · 원자화까지 분해? · CamelCase 일관? · `@dep:` 순환 없음(위상정렬)? · `[parallel]` 식별? · 4-space?
**PPR**: 복잡 노드에 def? · 입출력 타입? · 흐름제어 파이썬? · `AI_` snake_case+반환타입? · 결정론 로직은 실제 코드?
**자주 하는 실수**: Gantree만 작성(복잡 노드는 def 필수) / 트리에 로직 전부(흐름은 PPR로) / 5레벨 초과(decomposed) / 정확성에 `AI_`(실제 코드로).

---

## 부록 A — `AI_make_` 사역 패턴 (드물게 사용)

`AI_`는 AI가 직접 수행(자동사). 대상이 **스스로 상태를 바꾸도록 유발**할 때만 `AI_make_`(사역). 5번째 범주가 아니라 4범주 각각의 변형이다.

```python
keywords = AI_extract(text)           # AI가 추출
evolved  = AI_make_evolve(system)     # 시스템을 진화하게 만든다
consensus = AI_make_agree(agents, p)  # 에이전트들을 합의하게 만든다
```

**판단 순서**: ① 동사 주어가 AI 자신? → `AI_` ② 목적어가 스스로 상태 변경? → `AI_make_` ③ 모호 → `AI_`(보수적 기본값).

> 실무에서 대부분은 `AI_`로 충분하다. `AI_make_`는 자기진화·합의수렴·분화 같은 **명시적 사역 의미**가 핵심일 때만 쓴다. 접두사 체계는 절대 규칙이 아니라 진화 가능한 체계다(Co-evolutionary).
