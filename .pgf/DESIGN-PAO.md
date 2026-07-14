# PAO Design @v:0.5

## 1. 설계 목표

PAO는 이종 CLI 에이전트를 직접 결합하지 않고, 공통 `TaskContract`와 교체 가능한 포트를 사이에 둔 로컬 우선 오케스트레이션 시스템으로 설계한다.

핵심 원칙은 다음과 같다.

- 오케스트레이션 판단과 메시지 전송을 분리한다.
- LWAR runtime은 장기 세션으로 실행되고 내부 ADP가 mailbox를 반복 감시한다.
- OA는 LWAR를 직접 실행하거나 비대화형 명령을 요구하지 않는다.
- 실행 성공과 검증 성공을 분리한다.
- 모든 외부 효과는 정책과 감사 경계를 통과한다.
- MVP는 파일 기반이지만 핵심 계약은 전송 방식에 종속되지 않는다.
- 외부에는 안정적인 `LWARn`만 노출하고 실제 runtime/model은 동적 registry에서 해석한다.
- LWAR는 `lwar-runtime` 스킬을 로드하여 자기등록·lifecycle·Task 수행 계약을 이해한다.

## 2. Gantree

```text
PAO // 이종 CLI 에이전트 협업 오케스트레이션 시스템 (designing) @v:0.5
    ControlPlane // Claude Code CLI 기반 상위 Orchestrator (designing)
        GoalInterpreter // 사용자 목표와 완료 조건 정규화 (designing)
        TaskPlanner // Task DAG 생성과 재분해 (designing) @dep:GoalInterpreter
        RuntimeRouter // 능력·가용성·비용·위험 기반 런타임 선택 (designing) @dep:TaskPlanner
        WorkflowEngine // 상태 전이와 전체 실행 루프 제어 (designing) @dep:RuntimeRouter
    ValidationPlane // 결과를 증거 기반으로 판정하고 통합 (designing)
        ResultValidator // 스키마·완료 조건·테스트·산출물 증거 검증 (designing)
        CrossReviewer // 필요 시 다른 런타임에 독립 검토 위임 (designing)
        ResultSynthesizer // 검증된 부분 결과 통합 (designing) @dep:ResultValidator
    StatePlane // 태스크 수명주기와 실행 이력 관리 (designing)
        TaskRepository // Task와 Result 영속화 (designing)
        CoordinationRegistry // 자기등록·번호할당·lease·heartbeat·회수 관리 (designing)
        AuditJournal // 불변 이벤트와 정책 판정 기록 (designing)
    TransportPlane // 메시지 전달 구현을 포트 뒤에 격리 (designing)
        MessageTransportPort // publish·lease·ack·reject 공통 계약 (designing)
        FileTransport // 원자적 파일 쓰기 기반 MVP 전송 (designing)
    ExecutionPlane // 장기 LWAR 세션 내부의 ADP 실행 (designing)
        LWARPool // 동적으로 등록되는 이종 실행 런타임 풀 (designing)
        ADPResidentLoop // Watch·Receive·Execute·Report 반복 (designing)
        DeterministicTools // watcher·등록·결과 저장 Python 도구 (designing)
    PolicyPlane // 권한·재시도·라우팅 정책을 결정론적으로 집행 (designing)
        PermissionGate // read·write·network·command 허용 범위 판정 (designing)
        RetryPolicy // 오류 분류별 재시도·재위임·dead-letter 결정 (designing)
        RoutingPolicy // 런타임 선택 점수와 동시성 한도 관리 (designing)
    ObservabilityPlane // 운영 상태와 품질 추세 관측 (designing)
        StructuredLogger // 실행·오류·감사 로그 분리 (designing)
        OperationsView // 지표와 워크플로우·워커·dead-letter 조회 (designing)
```

## 3. Runtime Topology

### 3.1 Orchestrator

| 역할 | Runtime | Model candidates | 책임 |
|---|---|---|---|
| `orchestrator` | Claude Code CLI | Opus 4.8 / Fable 5 | 목표 해석, Task DAG 생성, LWAR 라우팅, 검증 지휘, 재계획, 최종 통합 |

Orchestrator는 직접 대규모 구현을 수행하기보다 **지휘·판정·복구**에 집중한다. 모델 선택은 `orchestrator_model` 설정으로 외부화하며, 두 모델을 동일한 제어 계약 아래에서 교체할 수 있게 한다.

### 3.2 Dynamic LWAR Pool

LWAR 목록은 미리 만들지 않는다. 각 runtime이 [`lwar-runtime` 스킬](../.agents/skills/lwar-runtime/SKILL.md)을 로드하고 `/lwar-register [number]` 요청을 제출하면, Orchestrator가 검증 후 `LWARn`을 할당한다. registry는 첫 승인 시 `var/registry/lwar_registry.json`으로 생성되는 파생 상태다.

초기 지원 대상은 다음과 같지만 번호와 runtime은 사전에 결합하지 않는다.

| Runtime candidate | Model | Interface |
|---|---|---|---|
| Codex | GPT 5.5 Sol | CLI |
| Antigravity | Gemini 3.5 Flash | agent runtime |
| Claude Code CLI | Opus 4.8 | CLI |
| Grok Build | Grok 4.5 | build runtime |
| Kimi CLI | Kimi Code 2.7 | CLI |
| DeepSeek TUI | DeepSeek V4 Pro | TUI |
| Qwen CLI | Qwen MAX 3.7 | CLI |
| OpenCode CLI | GLM 5.2 | CLI |

승인된 각 LWAR는 다음 identity/profile을 가진다.

```python
RuntimeProfile = {
    "lwar_id": str,
    "instance_id": str,
    "generation": int,
    "registry_version": int,
    "adapter_id": str,
    "runtime_name": str,
    "model": str,
    "vendor_family": str,
    "interface": Literal["cli", "tui", "agent", "build"],
    "capabilities": list[str],
    "supports_shell_tool": bool,
    "supports_long_session": bool,
    "max_parallel_jobs": int,
    "cost_class": str,
    "risk_class": str,
    "enabled": bool,
}
```

모델명은 PAO가 의미를 재해석하지 않는 사용자 지정 식별값이다. 실제 실행 가능 여부는 ADP heartbeat와 Task 수행 결과로 판정한다.

### 3.3 등록과 외부 명명 경계

```text
/lwar-register [number]
  → control/registration/requests/{request_id}.json
  → OA가 schema·collision·identity 검증
  → 자동이면 가장 작은 가용 번호를 원자 할당
  → control/registration/responses/{request_id}.json
  → LWAR가 승인된 identity를 채택하고 state=on
```

외부 메시지·파일·로그·artifact 경로에는 실제 provider 이름 대신 `LWARn`만 사용한다.

```text
mailbox/LWAR1/incoming/{message_id}.json
mailbox/LWAR1/claimed/{message_id}.json
mailbox/LWAR1/outgoing/{task_id}.result.json
mailbox/LWAR1/heartbeat.json
mailbox/LWAR1/leases/{task_id}.json
```

Orchestrator 내부의 `RuntimeRegistry`만 `LWARn → instance_id/generation/adapter/runtime/model` 매핑을 해석한다. `on`, `draining`, `off`는 번호를 점유하며 `deregistered` tombstone은 유예기간 후에만 재사용한다. 번호가 재사용되면 `generation`을 증가시켜 과거 실행과 구분한다.

### 3.4 검증 독립성 규칙

- 작성 runtime과 교차검토 runtime의 `lwar_id`는 달라야 한다.
- 중요 작업은 가능하면 `vendor_family`도 달라야 한다.
- Claude 계열 LWAR가 작업을 수행했다면 Orchestrator가 단독 승인하지 않고 비-Claude LWAR의 검토 결과를 요구한다.
- 동일 작업의 재위임은 이전 실패 runtime과 다른 `lwar_id`를 우선한다.
- 모든 선택 근거는 `routing_decision` 감사 이벤트로 기록한다.

## 4. 상위 컴포넌트 흐름

```text
User Goal
   │
   ▼
OA ── TaskContract ──► mailbox/LWARn/incoming
                              │
                              ▼
                 LWAR long-running session
                              │
                    ADP invokes Python watcher
                              │
           ┌──────────────────┼──────────────────┐
           ▼                  ▼                  ▼
      idle_timeout      task_received        shutdown
           │                  │                  │
        re-watch       AI executes Task       ADP stop
                              │
                              ▼
                 mailbox/LWARn/outgoing
                              │
                              ▼
                    OA ValidationPlane

PolicyPlane은 모든 route·lease·execute·retry 경계를 통제하고,
ObservabilityPlane은 전 과정을 correlation_id로 연결한다.
```

## 5. 핵심 계약

```python
TaskContract = {
    "schema_version": str,
    "task_id": str,
    "workflow_id": str,
    "parent_task_id": Optional[str],
    "role": str,
    "goal": str,
    "instructions": str,
    "completion_criteria": list[str],
    "cwd": str,
    "input_files": list[str],
    "expected_output": str,
    "required_capabilities": list[str],
    "lwar_hint": Optional[str],
    "instance_id": str,
    "generation": int,
    "registry_version": int,
    "review_independence": Literal["none", "runtime", "vendor"],
    "timeout_s": int,
    "max_retries": int,
    "priority": int,
    "permissions": dict,
    "adapter_options": dict,
}

ResultContract = {
    "schema_version": str,
    "task_id": str,
    "workflow_id": str,
    "lwar_id": str,
    "instance_id": str,
    "generation": int,
    "registry_version": int,
    "worker_id": str,
    "status": str,
    "summary": str,
    "evidence": dict,
    "artifacts": list[str],
    "next_action": str,
    "exit_code": int,
    "error": Optional[dict],
}
```

설계상 중요한 추가 필드는 `correlation_id`, `attempt`, `idempotency_key`, `lease_expires_at`이다. 이 네 필드는 중복 실행과 전체 흐름 추적을 단순화하므로 MVP부터 포함하는 편이 안전하다.

## 6. PPR

### 6.1 동적 등록과 번호 할당

```python
def approve_registration(request: RegistrationRequest, registry: Registry) -> RegistrationResponse:
    """자기등록 claim을 검증하고 LWARn을 원자 할당한다."""
    validate_schema(request)
    declared_profile = validate_profile_schema(request.profile)

    with registry.atomic_lock():
        if request.requested_lwar_id:
            candidate = request.requested_lwar_id
            if registry.is_occupied(candidate):
                return reject(request, "lwar_id_in_use")
        else:
            candidate = registry.lowest_available(
                occupied_states={"on", "draining", "off"},
                respect_tombstone_retention=True,
            )

        generation = registry.next_generation(candidate)
        binding = registry.bind(candidate, request.instance_id, generation, declared_profile)
        return accept(request, binding, initial_state="on")

    # acceptance_criteria:
    #   - registry lock 안에서 번호를 탐색하고 할당한다.
    #   - 자기신고 capability는 선언값이며 실제 가용성은 heartbeat와 Task 결과로 판정한다.
    #   - 번호 재사용 시 generation을 증가시킨다.
    #   - 승인 전 LWARn은 유효한 실행 identity가 아니다.
```

### 6.2 워크플로우 오케스트레이션

```python
def orchestrate_workflow(goal: str, policy: Policy) -> WorkflowResult:
    """목표를 분해하고 검증된 최종 결과까지 수렴시킨다."""
    intent = AI_understand_goal(goal)
    criteria = AI_derive_completion_criteria(intent)
    workflow = AI_decompose_task_dag(intent, criteria)

    validate_dag(workflow)
    persist_workflow(workflow)

    while not workflow.is_terminal():
        ready_tasks = get_dependency_ready_tasks(workflow)
        for task in ready_tasks:
            runtime = AI_select_runtime(task, get_runtime_profiles(), policy.routing)
            publish_with_policy_gate(task, runtime, policy)

        results = collect_available_results(workflow.id)
        for result in results:
            verdict = validate_result(result, workflow.task(result.task_id))
            apply_verdict(workflow, result, verdict, policy)

    return synthesize_verified_results(workflow)

    # acceptance_criteria:
    #   - 의존성이 충족된 태스크만 배정한다.
    #   - 검증을 통과하지 않은 결과는 succeeded로 확정하지 않는다.
    #   - 모든 상태 전이는 감사 이벤트를 남긴다.
    #   - terminal 상태는 succeeded, cancelled, dead_letter만 허용한다.
```

### 6.3 ADP 감시 슬라이스

```python
def run_adp(identity: LwarIdentity) -> None:
    """LWAR Agent 자신이 watcher를 반복 호출하며 같은 세션을 유지한다."""
    while True:
        event = run_python_watcher(identity, interval_s=1, timeout_s=90)
        if event.type in {"idle_timeout", "state_wait"}:
            continue
        if event.type == "control" and event.command == "shutdown":
            return
        if event.type == "task_received":
            result = AI_execute_task(event.task)
            submit_result_atomically(identity, event.task_id, result)

    # acceptance_criteria:
    #   - watcher만 종료되고 LWAR 세션은 유지된다.
    #   - 90초 timeout 후 watcher를 즉시 재호출한다.
    #   - incoming Task는 claimed로 원자 이동된다.
    #   - 결과 저장 뒤 반드시 watcher로 복귀한다.
```

### 6.4 결과 검증과 복구

```python
def validate_result(result: ResultContract, task: TaskContract) -> Verdict:
    """실행 종료가 아니라 계약과 증거를 기준으로 결과를 판정한다."""
    schema_ok = validate_schema(result)
    artifact_ok = verify_artifacts(result.artifacts, task.expected_output)
    evidence_ok = verify_evidence(result.evidence, task.completion_criteria)
    qualitative = AI_assess_completion(result.summary, task.completion_criteria)

    if schema_ok and artifact_ok and evidence_ok and qualitative.passed:
        return Verdict("passed", evidence=qualitative.evidence)
    return Verdict("rework", reason=collect_failures())

    # acceptance_criteria:
    #   - 결정론적으로 확인 가능한 항목은 코드로 검증한다.
    #   - AI 판단은 정성적 완료도 평가에만 사용한다.
    #   - 실패 이유는 다음 시도의 입력으로 보존한다.
```

```python
def recover_failed_task(task: TaskContract, failure: Failure, policy: Policy) -> RecoveryAction:
    """실패 유형에 따라 재시도, 재분해, 재위임 또는 격리를 선택한다."""
    if failure.kind in {"permission", "security"}:
        return RecoveryAction("human_gate")
    if task.attempt >= task.max_retries:
        return RecoveryAction("dead_letter")
    if failure.is_transient:
        return RecoveryAction("retry_same_runtime", backoff=exponential_backoff(task.attempt))
    if failure.kind == "scope_too_large":
        return RecoveryAction("replan_smaller_tasks")
    return RecoveryAction("reassign_runtime")

    # acceptance_criteria:
    #   - 동일 실패의 무한 반복을 금지한다.
    #   - 보안·권한 실패는 자동 재시도하지 않는다.
    #   - 재시도마다 이전 실패 원인과 변경 전략을 기록한다.
```

## 7. 권장 코드 구조

```text
PAO/
    pao_runtime/
        common.py          # atomic JSON·lock·mailbox
        registry.py        # registration·lifecycle 승인
        oa_cli.py          # OA send·collect·recover·control
        lwar_cli.py        # LWAR register·status·complete
        adp_watch.py       # 1초 polling·90초 watch slice
    .agents/skills/
        oa-runtime/
        lwar-runtime/
    control/
        registration/{requests,responses,archive}/
        lifecycle/{requests,responses,archive}/
    mailbox/
        LWARn/{incoming,claimed,outgoing,control,leases,work,archive,failed}/
    var/
        registry/
        identities/
    tests/
        test_adp_integration.py
```

`pao_runtime`은 결정론적 파일·상태 도구이고 실제 반복과 인지 작업은 LWAR Agent가 수행한다. `control`, `mailbox`, `var`는 실행 중 생성되는 상태다.

## 8. MVP 경계

첫 구현은 다음 수직 슬라이스 하나로 제한한다.

```text
Goal 입력
  → OA가 TaskContract 생성
  → mailbox/LWARn/incoming 게시
  → 상주 LWAR의 ADP watcher가 원자 claim
  → 같은 LWAR 세션이 Task 수행
  → Result 저장 후 ADP watcher 복귀
  → 결정론적 검증
  → 성공 또는 최대 1회 재시도
  → 최종 보고
```

MVP 포함:

- `TaskContract`·`ResultContract` JSON Schema
- `FileTransport`의 원자적 write/rename
- `lwar-runtime` 스킬과 `/lwar-register [number]` 계약
- 등록·응답·lifecycle·registry JSON Schema
- 첫 승인 시 동적 registry 생성과 `lowest_available` 원자 할당
- `oa-runtime`·`lwar-runtime` 행동 스킬
- 1초 polling·90초 timeout ADP watcher
- 비대화형 CLI 옵션 없는 TUI 지원
- heartbeat·lease·stale lease 회수
- 산출물 존재와 명령 종료 코드 검증
- correlation 기반 구조화 로그

MVP 제외:

- 복잡한 비용 최적화 라우팅
- 다중 호스트 실행
- Redis·MCP 전송
- shell tool 실행 자체를 지원하지 않는 runtime
- 웹 운영 콘솔
- 자율적 장기 메모리

## 9. 핵심 설계 결정

| 항목 | 결정 |
|---|---|
| 아키텍처 | Ports & Adapters 기반 modular monolith |
| Orchestrator | Claude Code CLI, Opus 4.8 / Fable 5 교체 가능 |
| 실행 풀 | 자기등록되는 이종 runtime의 동적 LWAR Pool |
| 외부 명칭 | 승인된 `LWARn`만 노출 |
| 내부 매핑 | Orchestrator가 생성하는 `var/registry/lwar_registry.json` |
| 자동 할당 | 가장 작은 가용 번호, atomic lock 안에서 결정 |
| 번호 재사용 | tombstone 유예 후 허용, `generation` 증가 |
| 상태 보장 | `at-least-once` + idempotency |
| 소스 오브 트루스 | MVP는 파일 이벤트·스냅샷, 이후 SQLite 확장 |
| 런타임 수명 | 사용자가 시작한 장기 LWAR 세션 + 내부 ADP |
| 성공 판정 | execute success와 validation success 분리 |
| 정책 집행 | AI 출력이 아닌 결정론적 `PolicyGate` |
| 확장 방식 | 공통 스킬과 mailbox 계약을 새 runtime이 직접 채택 |
| 운영 추적 | workflow/task/attempt correlation chain |
| 교차 검증 | 중요 작업은 다른 `vendor_family` 우선 |

가장 중요한 선택은 **분산 시스템처럼 사고하되, 먼저 modular monolith로 구현하는 것**이다. PAO는 파일을 쓰는 순간 이미 중복·부분 실패·재처리 문제를 가지므로 lease와 idempotency는 MVP 기능이지 후속 최적화가 아니다.

## 10. 설계 완료 기준

- Gantree가 제어·검증·상태·전송·실행·정책·관측 평면을 분리한다.
- 핵심 실행 흐름이 PPR과 검증 가능한 `acceptance_criteria`로 정의된다.
- `FileTransport`에서 MCP/SQLite로의 교체 경계가 명확하다.
- Claude Code CLI Orchestrator와 동적 LWAR Pool의 역할 경계가 명확하다.
- 외부 계약이 실제 provider/model 이름에 종속되지 않는다.
- `(lwar_id, generation, instance_id)` 조합으로 과거 실행을 식별할 수 있다.
- LWAR가 스킬을 통해 등록·상태·Task 수행 계약을 이해한다.
- 비대화형 실행 없이 ADP가 TUI/CLI 세션 내부에서 메시지를 수신한다.
- 동일 계열 자기검증을 억제하는 독립성 규칙이 정의된다.
- MVP의 포함·제외 범위가 구분된다.
- 기술서의 task/result/heartbeat/lease/validation 원칙과 충돌하지 않는다.
