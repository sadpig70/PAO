# PAO ADP 운영 가이드

## 구성요소

- `OA`: Task 계획·등록 승인·메시지 게시·결과 검증
- `LWAR`: 장기 실행되는 이종 Agent runtime
- `ADP`: LWAR 세션 내부의 `Watch → Execute → Report` 루프
- `pao_runtime`: 파일 I/O와 상태 전이를 강제하는 Python 도구

## 최초 구동

### 1. LWAR 세션 시작

각 runtime을 평소 대화형 방식으로 실행하고 `.agents/skills/lwar-runtime/SKILL.md`를 로드한다.

### 2. LWAR 등록 요청

```bash
python -m pao_runtime.lwar_cli register --runtime-name "Runtime" --model "Model" --adapter-id runtime --vendor-family vendor --interface tui
```

### 3. OA 승인

```bash
python -m pao_runtime.oa_cli reconcile
```

### 4. LWAR identity 채택

```bash
python -m pao_runtime.lwar_cli response REQUEST_ID
```

### 5. ADP 시작

```bash
python -m pao_runtime.adp_watch --identity-file IDENTITY_FILE --interval 1 --timeout 90
```

LWAR는 stdout event를 처리한 후 같은 명령으로 반드시 복귀한다.

## 정상 작업 흐름

```text
OA task draft
  → oa_cli send
  → mailbox/LWARn/incoming
  → adp_watch atomic claim
  → stdout task_received
  → LWAR 작업 수행
  → lwar_cli complete
  → mailbox/LWARn/outgoing
  → oa_cli collect
  → OA 검증
```

## Runtime별 차이

모든 runtime은 같은 Python 명령과 mailbox 계약을 사용한다. 차이는 각 runtime에서 스킬을 로드하고 shell 명령을 실행하는 UI 방식뿐이다. 비대화형 실행 옵션은 요구하지 않는다.

## 운영 주의

- `var/`, `mailbox/`, `control/`은 실행 중 생성되는 상태다.
- source control에는 실행 상태를 커밋하지 않는다.
- idle timeout은 오류가 아니다.
- context 오염을 막기 위해 idle event를 설명하지 않고 즉시 재호출한다.
- heartbeat와 lease를 기반으로 장애를 판단한다.
