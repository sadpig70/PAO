---
name: lwar-runtime
description: "PAO LWAR self-registration and ADP (Agent Daemon Process) resident loop contract. Load on /lwar-register, /lwar-status, /lwar-on, /lwar-drain, /lwar-off, /lwar-unregister, or whenever an assigned LWAR must watch its mailbox and execute OA tasks."
user-invocable: true
argument-hint: "register [number] | adp | status | on | drain | off | unregister"
---

# LWAR Runtime Skill v2 — ADP

> ADP는 **Agent Daemon Process**다. 이미 실행된 LWAR 세션이 Python watcher를 반복 호출하여 자기 mailbox를 수신하고, 작업 수행 후 결과를 저장한 뒤 다시 watcher로 돌아가는 상주 루프다.

## 1. 절대 규칙

1. 이 스킬과 [`references/adp-contract.md`](references/adp-contract.md)를 전체 읽는다.
2. 승인된 `(lwar_id, instance_id, generation)`만 자기 정체성으로 사용한다.
3. 외부 프로세스가 LWAR를 재실행할 것으로 기대하지 않는다. 현재 세션 안에서 ADP를 지속한다.
4. `idle_timeout`과 `state_wait`에서는 설명을 생성하지 말고 즉시 같은 watcher를 재호출한다.
5. `task_received`에서는 TaskContract 권한 안에서 수행하고 반드시 `complete`로 결과를 제출한다.
6. 결과 제출 후 즉시 watcher로 복귀한다.
7. `shutdown`만 ADP 종료 신호다.

## 2. 명령 계약

| 사용자 명령 | 행동 |
|---|---|
| `/lwar-register` | 자동 번호 등록 요청 |
| `/lwar-register 5` | `LWAR5` 지정 요청 |
| `/lwar-adp` | 승인된 identity로 ADP 시작 |
| `/lwar-status` | registry·heartbeat 확인 |
| `/lwar-on` | `on` 전이 요청 |
| `/lwar-drain` | `draining` 전이 요청 |
| `/lwar-off` | `off` 전이 요청 |
| `/lwar-unregister` | `off` 이후 `deregistered` 요청 |

`/lwar-regite`는 `/lwar-register`의 오타 호환 alias다.

## 3. 등록

실제 runtime 정보만 사용하며 모르는 값을 추측하지 않는다.

```bash
python -m pao_runtime.lwar_cli register \
  --runtime-name "Codex" \
  --model "GPT 5.5 Sol" \
  --adapter-id codex \
  --vendor-family openai \
  --interface cli \
  --capability coding \
  --capability testing
```

stdout의 `request_id`를 기억한다. OA가 승인한 뒤 다음을 실행한다.

```bash
python -m pao_runtime.lwar_cli response REQUEST_ID
```

`event=identity_adopted`일 때 출력된 `identity_file`이 이후 ADP의 유일한 identity 입력이다. `pending`이면 승인된 것으로 간주하지 않는다.

## 4. ADP 중심 루프

```python
def ADP(identity_file: Path) -> None:
    while True:
        event = run("python -m pao_runtime.adp_watch --identity-file", identity_file)
        if event.event in {"idle_timeout", "state_wait"}:
            continue
        if event.event == "adp_error":
            report_error_and_stop(event)
        if event.event == "control":
            if event.command == "shutdown": return
            handle_control(event)
            continue
        if event.event == "task_received":
            result = AI_execute_task(event.task)
            write_result_draft(result)
            run_lwar_complete(identity_file, event.task_id, result.file)
            continue

    # acceptance_criteria:
    #   - watcher timeout은 LWAR 세션 종료가 아니다.
    #   - timeout 후 다른 추론 없이 watcher를 다시 실행한다.
    #   - Task 수신과 결과 제출 사이에 다른 Task를 claim하지 않는다.
    #   - 성공·실패·blocked 모두 ResultContract로 제출한다.
```

Watcher 기본 호출:

```bash
python -m pao_runtime.adp_watch \
  --identity-file IDENTITY_FILE \
  --interval 1 \
  --timeout 90 \
  --lease-seconds 180
```

## 5. stdout event 처리

| `event` | 즉시 행동 |
|---|---|
| `idle_timeout` | 같은 watcher 재호출 |
| `state_wait` | 같은 watcher 재호출; Task 수행 금지 |
| `task_received` | `task` 수행 → 결과 제출 |
| `control:ping` | watcher 재호출 |
| `control:drain` | 현재 작업 완료 후 lifecycle `draining` 요청 |
| `control:cancel` | 해당 Task 중단·cancelled 결과 제출 |
| `control:shutdown` | ADP 종료 |
| `adp_error` | 오류 보고 후 ADP 중지 |

## 6. 작업 수행과 결과 제출

- `cwd`, `permissions`, `completion_criteria`를 먼저 확인한다.
- Task가 허용하지 않은 경로·명령·network를 사용하지 않는다.
- 정확한 검증은 실제 명령과 코드로 수행하고 근거를 `evidence`에 기록한다.
- 결과 초안은 `mailbox/LWARn/work/{task_id}/result.json`에 작성한다.

결과 초안:

```json
{
  "status": "succeeded",
  "summary": "작업 요약",
  "evidence": {"commands": [], "tests_passed": 0, "tests_failed": 0},
  "artifacts": [],
  "next_action": "validate",
  "exit_code": 0,
  "error": null
}
```

제출:

```bash
python -m pao_runtime.lwar_cli complete \
  --identity-file IDENTITY_FILE \
  --task-id TASK_ID \
  --result-file mailbox/LWARn/work/TASK_ID/result.json
```

`event=result_submitted`을 확인한 후 watcher를 재호출한다.

## 7. Lifecycle

```bash
python -m pao_runtime.lwar_cli state draining --identity-file IDENTITY_FILE
python -m pao_runtime.lwar_cli state off --identity-file IDENTITY_FILE
python -m pao_runtime.lwar_cli state on --identity-file IDENTITY_FILE
python -m pao_runtime.lwar_cli state deregistered --identity-file IDENTITY_FILE
```

요청 후 OA reconcile과 `/lwar-status` 확인 전에는 상태가 확정됐다고 간주하지 않는다. `deregistered`는 `off`에서만 요청한다.

## 8. 금지 행동

- 승인 전에 `LWARn`을 자칭하지 않는다.
- registry·incoming·lease 파일을 직접 수정하지 않는다.
- idle stdout을 장문으로 재서술하여 context를 오염시키지 않는다.
- claimed Task를 결과 없이 방치하지 않는다.
- ADP 중에 사용자 또는 OA의 `shutdown` 없이 자의적으로 종료하지 않는다.
