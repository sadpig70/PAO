# ADP Runtime Contract v1

## 정의

**ADP — Agent Daemon Process**는 LWAR 세션 내부의 상주 제어 루프다. Python watcher는 결정론적 I/O 도구이고, ADP의 실제 반복 주체는 LWAR Agent다.

```text
Watch(≤90s) → stdout event → Agent decision
    idle/state_wait ────────────────┐
    task → execute → submit result ─┤→ Watch
    control → handle ───────────────┘
    shutdown → stop
```

## Mailbox

```text
mailbox/LWARn/
    incoming/          # OA Task 게시
    claimed/           # ADP가 원자 점유한 Task
    outgoing/          # LWAR Result 게시
    control/           # OA control 게시
    control_claimed/   # watcher의 순간 점유 영역
    leases/            # 실행 lease
    work/              # LWAR 작업 중간 파일
    heartbeat.json
    archive/
        tasks/
        results/
        control/
    failed/
```

파일 작성은 항상 임시 파일 → flush/fsync → `os.replace` 순서다. Task 수신은 `incoming → claimed` 원자 이동으로 확정한다.

## Exit code

| Code | Event | Agent 행동 |
|---:|---|---|
| `0` | `task_received` | Task 수행 |
| `10` | `idle_timeout`, `state_wait` | 즉시 watcher 재호출 |
| `20` | `control` | 명령 처리 |
| `30` | `adp_error` | 보고 후 중지 |

Agent는 exit code만 믿지 않고 stdout JSON의 `event`도 함께 확인한다.

## 장애 복구

- watcher가 죽어도 LWAR 세션은 같은 명령을 재호출할 수 있다.
- LWAR 세션이 죽으면 heartbeat가 stale해진다.
- OA `recover`는 만료 lease의 claimed Task를 incoming으로 되돌린다.
- 동일 `task_id` Result가 이미 있으면 재수행 결과를 자동 승인하지 않는다.
- 번호가 재사용돼도 `generation`과 `instance_id`가 다른 메시지는 watcher가 거부한다.
