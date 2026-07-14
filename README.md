![PAO — Persistent Agent Orchestration](assets/PAO_hero.png)

# PAO

**Persistent Agent Orchestration** — 서로 다른 장기 실행 AI runtime을 `LWARn`이라는 단일 외부 identity와 파일 기반 메시지 버스로 조율하는 로컬 오케스트레이션 시스템입니다.

PAO는 vendor CLI를 강제로 비대화형 실행하지 않습니다. 사용자가 시작한 각 runtime 세션이 **ADP(Agent Daemon Process)** watcher를 반복 호출하여 작업을 수신하고, 같은 대화 문맥에서 실행 결과를 반환합니다.

## Architecture

```text
OA (Orchestration Agent)
  └─ Task JSON → mailbox/LWARn/incoming/
                         ↓ atomic claim
              LWAR long-running session
                         ↓ ADP watch/execute loop
  └─ Result JSON ← mailbox/LWARn/outgoing/
```

- **OA** — 등록 승인, Task 게시, control 전달, Result 회수 및 lease 복구
- **LWAR** — provider/model 이름을 감춘 실행 identity (`LWAR1`, `LWAR2`, ...)
- **ADP** — 1초 polling과 90초 watch slice를 반복하는 resident mailbox loop
- **File bus** — 원자 JSON publish/claim, heartbeat, generation, lease 기반 전달

## Key properties

- `/lwar-register [number]` 자기등록과 최저 가용 번호 자동 할당
- `lwar_id + instance_id + generation`으로 stale message 격리
- `on → draining → off → deregistered` lifecycle
- TUI를 포함한 장기 실행 runtime 지원
- provider 비종속 Task/Result contract
- 만료 lease 복구와 alias 재사용 generation 증가

## Quick start

### 1. LWAR registration

```bash
python scripts/lwar.py register \
  --runtime-name "Runtime" \
  --model "Model" \
  --adapter-id runtime \
  --vendor-family vendor \
  --interface tui \
  --root .
```

숫자를 지정하려면 `register 1`처럼 입력합니다. 생략하면 OA가 가장 작은 가용 번호를 할당합니다.

### 2. OA approval

```bash
python scripts/oa.py reconcile --root .
python scripts/lwar.py response <request_id> --root .
```

### 3. ADP watch slice

```bash
python scripts/adp_watch.py \
  --identity-file <identity_file> \
  --root . \
  --interval 1 \
  --timeout 90
```

`idle_timeout` 또는 `state_wait`이면 같은 LWAR 세션이 watcher를 즉시 다시 호출합니다. `task_received`이면 Task를 수행하고 `lwar.py complete`로 Result를 제출합니다.

## Documentation

- [Technical specification](docs/PAO_TechSpec.md)
- [ADP operations guide](docs/PAO_ADP_Operations.md)
- [Runtime bootstrap prompts](docs/LWAR_ADP_Bootstrap.md)
- [Canonical architecture](.pgf/DESIGN-PAO.md)
- [ADP design](.pgf/DESIGN-PAOADP.md)
- [Verification review](.pgf/REVIEW-PAOADP.md)

## Verification

```bash
python -m unittest discover -s tests -v
python -m py_compile pao_runtime/*.py scripts/*.py tests/*.py
```

현재 통합 테스트는 등록, 충돌 거부, 전체 Task/Result 흐름, idle timeout, off-state 차단, stale lease 복구, shutdown control 및 generation 증가를 검증합니다.
