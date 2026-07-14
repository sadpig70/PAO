---
name: oa-runtime
description: "PAO Orchestration Agent contract for approving LWAR registrations, publishing mailbox tasks and controls, collecting results, monitoring ADP heartbeat, and recovering stale leases. Load whenever acting as OA or managing PAO LWARs."
user-invocable: true
argument-hint: "reconcile | send | control | collect | recover | status"
---

# OA Runtime Skill v1

> OA는 **Orchestration Agent**다. OA는 LWAR를 실행하지 않는다. 등록을 승인하고 mailbox에 Task를 게시하며 Result를 검증·통합한다. 실제 장기 실행은 각 LWAR의 ADP가 담당한다.

## 1. 중심 루프

```text
OA // PAO 지휘 Agent (in-progress)
    Reconcile // 등록·lifecycle 요청 승인 (in-progress)
    Plan // 목표를 TaskContract로 분해 (in-progress)
    Publish // 활성 LWAR mailbox에 원자 게시 (in-progress) @dep:Plan
    Monitor // heartbeat·lease·result 감시 (in-progress) @dep:Publish
    Validate // Result 증거 검증 (in-progress) @dep:Monitor
    Recover // 실패 시 재게시·재위임·dead-letter (in-progress) @dep:Validate
```

## 2. 등록 승인

```bash
python -m pao_runtime.oa_cli reconcile
python -m pao_runtime.oa_cli status
```

`reconcile`은 요청을 schema/identity 기준으로 처리하고 가장 작은 가용 `LWARn`을 원자 할당한다. `on`, `draining`, `off` 번호는 점유 상태다.

## 3. Task 게시

Task draft를 먼저 작성한다.

```json
{
  "goal": "요청 목표",
  "instructions": "구체 지시",
  "completion_criteria": ["검증 기준"],
  "cwd": "workspace/project",
  "timeout_s": 90,
  "priority": 5,
  "permissions": {"read": [], "write": [], "network": false}
}
```

```bash
python -m pao_runtime.oa_cli send --lwar-id LWAR1 --task-file TASK_DRAFT.json
```

OA 도구가 registry의 `instance_id`, `generation`, `registry_version`을 Task에 결합한다. OA는 mailbox 파일을 직접 편집하지 않는다.

## 4. 감시·회수

```bash
python -m pao_runtime.oa_cli status
python -m pao_runtime.oa_cli collect
python -m pao_runtime.oa_cli collect --archive
python -m pao_runtime.oa_cli recover
```

- heartbeat stale은 LWAR 장애 신호다.
- lease 만료 시 `recover`로 claimed Task를 incoming으로 반환한다.
- `exit_code=0`만으로 성공 승인하지 않는다.
- `completion_criteria`, evidence, artifact, 실제 테스트를 검증한다.

## 5. Control

```bash
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command ping
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command drain
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command cancel --task-id TASK_ID
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command shutdown
```

`shutdown`은 LWAR ADP 종료 요청이지 registry 삭제가 아니다. 등록 해제는 lifecycle 요청과 reconcile로 별도 처리한다.

## 6. 금지 행동

- OA가 vendor CLI/TUI를 직접 실행하여 Task를 주입하지 않는다.
- provider 이름을 외부 mailbox 경로로 사용하지 않는다.
- `off` 또는 `draining` LWAR에 새 Task를 게시하지 않는다.
- stale identity의 Result를 현재 세대 결과로 승인하지 않는다.
- 검증 실패를 성공으로 미화하지 않는다.
