# PAO ADP Design @v:1.0

## Gantree

```text
PAOADP // OA와 LWAR를 연결하는 Agent Daemon Process 시스템 (done) @v:1.0
    Protocol // 메시지·identity·상태 계약 (done)
        DynamicRegistration // LWAR 자기등록과 번호 할당 (done)
        MailboxContract // 메시지별 파일 queue와 원자 점유 (done)
        LifecycleContract // on·draining·off·deregistered 전이 (done)
    OATools // OA의 결정론적 제어 도구 (done) @dep:Protocol
        ReconcileService // 등록·상태 요청 승인 (done)
        MessagePublisher // Task·control 메시지 게시 (done)
        ResultCollector // 결과 회수와 stale lease 복구 (done)
    ADPRuntime // LWAR 내부 상주 자기감시 루프 (done) @dep:Protocol
        MessageWatcher // 1초 polling·90초 timeout·stdout event (done)
        AtomicClaim // incoming에서 claimed로 원자 이동 (done)
        HeartbeatLease // 생존 상태와 작업 lease 기록 (done)
        ResultSubmitter // 수행 결과 저장과 Task archive (done)
    RuntimeSkills // Agent가 행동 계약을 이해하는 문서 (done) @dep:OATools,ADPRuntime
        OASkill // OA 등록·게시·회수 지침 (done)
        LWARSkill // 등록·ADP loop·작업 수행 지침 (done)
    Verification // 계약·코드·통합 검증 (done) @dep:RuntimeSkills
        UnitTests // 등록·watch·result·lifecycle 테스트 (done)
        ArchitectureGate // 기존 WorkerSupervisor 모델 제거 확인 (done)
```

## PPR

```python
def run_adp(identity: LwarIdentity, policy: ADPPolicy) -> None:
    """LWAR 세션 내부에서 watcher를 반복 호출한다."""
    while identity.state != "deregistered":
        event = run_python_watcher(identity, interval_s=1, timeout_s=90)
        if event.type in {"idle_timeout", "state_wait"}:
            continue
        if event.type == "control" and event.command == "shutdown":
            break
        if event.type == "task_received":
            result = AI_execute_task(event.task)
            submit_result_atomically(identity, event.task, result)

    # acceptance_criteria:
    #   - watcher만 종료되고 LWAR 세션은 유지된다.
    #   - timeout 후 즉시 watcher를 재호출한다.
    #   - task 완료 후 결과 저장 뒤 watcher로 복귀한다.
    #   - shutdown 외 control을 작업으로 오인하지 않는다.
```

```python
def deliver_task(oa: OA, identity: LwarIdentity, task: TaskContract) -> Path:
    """OA가 활성 LWAR mailbox에 불변 Task 파일을 게시한다."""
    assert identity.state == "on"
    task.bind(identity.lwar_id, identity.instance_id, identity.generation)
    return atomic_write(identity.mailbox / "incoming" / task.filename, task)

    # acceptance_criteria:
    #   - provider 이름이 경로에 노출되지 않는다.
    #   - 부분 파일을 watcher가 읽을 수 없다.
    #   - 하나의 message_id가 한 번만 claim된다.
```
