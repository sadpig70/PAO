# LWAR ADP Bootstrap

이 문서는 장시간 실행 중인 각 AI runtime 세션에 최초 1회 전달하는 공통 부트스트랩이다. 외부에서는 provider 이름이 아니라 배정된 `LWARn`만 사용한다.

## 공통 전달문

```text
당신은 PAO의 LWAR이다. 저장소 루트의 AGENTS.md와
.agents/skills/lwar-runtime/SKILL.md를 전부 읽고 그 계약을 따른다.

1. 아래의 register 명령을 실행한다. 숫자가 없으면 빈 LWAR 번호를 요청한다.
2. stdout의 request_id를 기억하고 OA가 승인할 때까지 기다린다.
3. OA 승인 후 response 명령으로 identity file을 채택한다.
4. 동일한 장기 세션에서 adp_watch.py를 반복 실행한다.
5. idle_timeout 또는 state_wait이면 즉시 watcher를 다시 실행한다.
6. task_received이면 task를 수행하고 Result JSON을 만든 뒤 lwar.py complete를 실행한다.
7. control.shutdown이면 ADP 반복을 끝낸다. 그 외에는 skill의 control 규칙을 따른다.
8. provider/model 이름을 mailbox 경로나 OA 메시지에 노출하지 않는다.

OA 명령이나 다른 LWAR의 mailbox를 직접 조작하지 않는다.
```

## Runtime별 register 명령

숫자를 지정하려면 `register` 바로 뒤에 번호를 추가한다. 예: `register 3 ...`.

```bash
# Codex CLI / GPT 5.5 Sol
python scripts/lwar.py register --runtime-name "Codex CLI" --model "GPT 5.5 Sol" --adapter-id codex --vendor-family openai --interface cli --capability code --capability shell --root .

# Antigravity / Gemini 3.5 Flash
python scripts/lwar.py register --runtime-name "Antigravity" --model "Gemini 3.5 Flash" --adapter-id antigravity --vendor-family google --interface agent --capability code --root .

# Claude Code CLI / Opus 4.8
python scripts/lwar.py register --runtime-name "Claude Code CLI" --model "Opus 4.8" --adapter-id claude-code --vendor-family anthropic --interface cli --capability code --capability shell --root .

# Grok Build / Grok 4.5
python scripts/lwar.py register --runtime-name "Grok Build" --model "Grok 4.5" --adapter-id grok-build --vendor-family xai --interface build --capability code --root .

# Kimi CLI / Kimi Code 2.7
python scripts/lwar.py register --runtime-name "Kimi CLI" --model "Kimi Code 2.7" --adapter-id kimi --vendor-family moonshot --interface cli --capability code --capability shell --root .

# DeepSeek TUI / DeepSeek V4 Pro
python scripts/lwar.py register --runtime-name "DeepSeek TUI" --model "DeepSeek V4 Pro" --adapter-id deepseek --vendor-family deepseek --interface tui --capability code --root .

# Qwen CLI / Qwen MAX 3.7
python scripts/lwar.py register --runtime-name "Qwen CLI" --model "Qwen MAX 3.7" --adapter-id qwen --vendor-family alibaba --interface cli --capability code --capability shell --root .

# OpenCode CLI / GLM 5.2
python scripts/lwar.py register --runtime-name "OpenCode CLI" --model "GLM 5.2" --adapter-id opencode --vendor-family zhipu --interface cli --capability code --capability shell --root .
```

## 승인과 ADP 진입

OA가 등록 요청을 처리한다.

```bash
python scripts/oa.py reconcile --root .
```

LWAR는 자기 `request_id`로 identity를 채택한다. 출력된 `identity_file`을 다음 명령에 사용한다.

```bash
python scripts/lwar.py response <request_id> --root .
python scripts/adp_watch.py --identity-file <identity_file> --root . --interval 1 --timeout 90
```

`adp_watch.py`는 daemon 자체가 아니라 한 번의 감시 slice다. 장기 생명주기는 AI runtime 세션이 보유하며, 매 stdout event 뒤 다음 행동을 판단하고 watcher를 다시 호출한다.

## 작업 완료 Result 예시

```json
{
  "status": "succeeded",
  "summary": "요청한 작업을 완료했다.",
  "evidence": {"tests": ["python -m unittest discover -s tests -v"]},
  "artifacts": ["path/to/output.md"],
  "next_action": "validate"
}
```

```bash
python scripts/lwar.py complete --identity-file <identity_file> --task-id <task_id> --result-file <result.json> --root .
```
