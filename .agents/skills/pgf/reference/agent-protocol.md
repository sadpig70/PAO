# PGF Agent Protocol — PG 기반 에이전트 간 소통 규격

> 에이전트를 파견할 때 자연어 프롬프트 대신 **PG 명세**를 전달한다.
> AI가 AI에게 태스크를 위임할 때의 공통 언어.

---

## 1. 왜 PG로 소통하는가

| 자연어 프롬프트 | PG 태스크 명세 |
|---|---|
| 의도가 모호할 수 있음 | `def` 시그니처로 입출력 명확 |
| 검증 기준이 암묵적 | `acceptance_criteria` 내장 |
| 실행 순서가 산문에 묻힘 | `@dep:`, `→`로 구조화 |
| 실패 대응 불명확 | Failure Strategy 명시 |
| 결과 형식 불통일 | `-> ReturnType`으로 반환 계약 |

PG로 소통하면:
- **파견 AI**가 의도를 정확히 전달
- **수행 AI**가 acceptance_criteria로 자기 검증 가능
- **결과**가 타입화되어 통합이 용이

---

## 2. TaskSpec — 에이전트 파견 명세 형식

에이전트에게 전달하는 태스크를 다음 PG 구조로 작성한다:

```python
def task_name(
    # 입력 파라미터 — 수행에 필요한 모든 컨텍스트
    target_crate: Path,
    existing_pattern: Path,      # 참고할 기존 코드
    workspace_root: Path = "D:\\project\\ocwr",
) -> TaskResult:
    """한 줄 태스크 설명"""

    # context: (수행 AI가 먼저 읽어야 할 파일/정보)
    #   - Read(target_crate / "src/lib.rs")
    #   - Read(existing_pattern)

    # steps:
    #   1. 기존 패턴 분석
    #   2. 새 모듈 구현
    #   3. 테스트 작성
    #   4. cargo check + clippy

    # implementation:
    AI_implement_following_pattern(existing_pattern, target="new_module")

    # acceptance_criteria:
    #   - cargo check -p {crate} → 0 errors
    #   - cargo clippy -p {crate} -- -D warnings → 0 warnings
    #   - tests >= N개
    #   - 기존 테스트 불변

    # failure_strategy:
    #   - 컴파일 에러 → AI_fix_compile_error(error_msg)
    #   - clippy 경고 → AI_fix_clippy_warning(warning_msg)
    #   - max_retry: 3

    # return:
    #   TaskResult = {
    #       files_created: list[Path],
    #       files_modified: list[Path],
    #       test_count: int,
    #       summary: str,
    #   }
```

### 필수 섹션

| 섹션 | 역할 | 필수 |
|---|---|---|
| `def` 시그니처 | 입력 파라미터 + 반환 타입 | ✅ |
| `"""docstring"""` | 한 줄 태스크 설명 | ✅ |
| `# context:` | 수행 전 읽어야 할 파일 | ✅ |
| `# acceptance_criteria:` | 완료 판정 기준 | ✅ |
| `# steps:` | 실행 순서 (선택적) | ○ |
| `# implementation:` | 핵심 로직 (AI_ 또는 실제 코드) | ○ |
| `# failure_strategy:` | 실패 시 대응 | ○ |
| `# return:` | 결과 구조 | ○ |

---

## 3. 병렬 파견 — [parallel] TaskSpec

여러 에이전트를 동시 파견할 때:

```python
[parallel]

def implement_discord_adapter(channels: Path) -> AdapterResult:
    """Discord REST API 어댑터"""
    # context: Read(channels / "adapters/slack.rs")
    # acceptance_criteria: cargo check, tests >= 10

def implement_slack_adapter(channels: Path) -> AdapterResult:
    """Slack Web API 어댑터"""
    # context: Read(channels / "adapters/discord.rs")  # 먼저 완성된 것 참조
    # acceptance_criteria: cargo check, tests >= 10

def implement_telegram_adapter(channels: Path) -> AdapterResult:
    """Telegram Bot API 어댑터"""
    # context: Read(channels / "adapter.rs")
    # acceptance_criteria: cargo check, tests >= 10

[/parallel]

# 통합 검증 — 모든 병렬 태스크 완료 후
def verify_all_adapters(workspace: Path) -> VerifyResult:
    """전체 어댑터 통합 검증"""
    # @dep: implement_discord_adapter, implement_slack_adapter, implement_telegram_adapter
    cargo_check(workspace, "--workspace")
    cargo_test(workspace, "-p ocwr_channels")
```

---

## 4. 의존 체인 파견 — @dep TaskSpec

순서가 있는 태스크 위임:

```python
def expand_adapter_traits(channels: Path) -> TraitResult:
    """채널 어댑터 인터페이스 7종 추가"""
    # acceptance_criteria: AdapterKind.TOTAL == 16

def implement_adapters(channels: Path) -> list[AdapterResult]:
    """6종 채널 어댑터 구현"""
    # @dep: expand_adapter_traits
    # ↑ trait 확장이 완료된 후에만 실행
    [parallel]
    AI_implement("discord")
    AI_implement("slack")
    AI_implement("telegram")
    [/parallel]
```

---

## 5. 결과 보고 형식 — TaskResult

수행 AI가 반환하는 결과도 PG 구조를 따른다:

```python
# 성공 결과
TaskResult = {
    "status": "done",
    "files_created": ["src/adapters/discord.rs"],
    "files_modified": ["src/adapters/mod.rs", "src/lib.rs"],
    "test_count": 12,
    "summary": "Discord REST API adapter: send/embed/react/delete + 12 tests",
    "acceptance": {
        "cargo_check": "pass",
        "clippy": "pass",
        "tests": "12/12 pass",
    },
}

# 실패 결과
TaskResult = {
    "status": "blocked",
    "blocker": "reqwest crate not in workspace dependencies",
    "attempted_fix": "Added reqwest to Cargo.toml but version conflict with existing dep",
    "suggestion": "Upgrade workspace reqwest from 0.11 to 0.12",
}
```

---

## 6. Orchestrator → Agent 흐름

PGF의 execute 단계에서 `[parallel]` 블록을 만나면:

```python
def orchestrate_parallel_block(nodes: list[GantreeNode], design: Path):
    """[parallel] 블록의 노드들을 에이전트로 파견"""

    for node in nodes:
        # 1. 노드의 PPR def 또는 # 주석에서 TaskSpec 추출
        task_spec = extract_task_spec(node, design)

        # 2. PG 형식의 TaskSpec을 에이전트 프롬프트로 전달
        agent_prompt = format_pg_task_spec(task_spec)

        # 3. Agent 도구로 파견
        Agent(
            prompt=agent_prompt,
            name=node.name,
            mode="bypassPermissions",
            run_in_background=True,
        )

    # 4. 모든 에이전트 완료 대기
    # 5. 각 에이전트의 TaskResult 수집
    # 6. acceptance_criteria 교차 검증
```

### PG TaskSpec → Agent 프롬프트 변환 규칙

```python
def format_pg_task_spec(spec: TaskSpec) -> str:
    """PG TaskSpec을 에이전트가 이해할 프롬프트로 변환

    핵심: PG 구조를 유지하면서 실행 가능한 지시로 변환.
    자연어 설명은 최소화하고, PG 명세가 지시의 본체.
    """
    prompt = f"""You are executing a PG TaskSpec.

## TaskSpec

```python
{spec.to_pg_string()}
```

## Execution Rules
1. Read files listed in `# context:` first
2. Follow `# steps:` in order
3. Verify against `# acceptance_criteria:` before reporting done
4. On failure, apply `# failure_strategy:`
5. Return result in TaskResult format
"""
    return prompt
```

---

## 7. 실전 예시 — 이전 세션의 자연어 → PG 변환

### Before (자연어 프롬프트)

```
Implement a Discord channel adapter for the OCWR Rust project at D:\openclaw\ocwr.
The channel adapter framework is in crates/ocwr_channels/src/adapter.rs — read it first...
Create DiscordAdapter struct that contains bot token, HTTP client, Discord API base URL...
Implement send_message, send_embed, add_reaction, delete_message...
Add unit tests, run cargo check, run clippy...
```

### After (PG TaskSpec)

```python
def implement_discord_adapter(
    channels_crate: Path = "D:\\openclaw\\ocwr\\crates\\ocwr_channels",
) -> AdapterResult:
    """Discord REST API 채널 어댑터 구현"""

    # context:
    #   - Read(channels_crate / "src/adapter.rs")      # trait 타입 확인
    #   - Read(channels_crate / "src/adapters/mod.rs")  # 등록 패턴
    #   - Read(channels_crate / "src/message.rs")       # 메시지 타입

    # implementation:
    adapter = DiscordAdapter(
        config: DiscordConfig = {bot_token: str, guild_id: str, command_prefix: Optional[str]},
        client: reqwest.Client,
        base_url: str = "https://discord.com/api/v10",
    )

    methods = [
        send_message(channel_id: str, content: str) -> DiscordMessage,
        send_embed(channel_id: str, embed: DiscordEmbed) -> DiscordMessage,
        add_reaction(channel_id: str, message_id: str, emoji: str) -> None,
        delete_message(channel_id: str, message_id: str) -> None,
        get_guild_channels(guild_id: str) -> list[DiscordChannel],
        get_guild_members(guild_id: str) -> list[DiscordUser],
    ]

    # acceptance_criteria:
    #   - cargo check -p ocwr_channels → 0 errors
    #   - cargo clippy -p ocwr_channels -- -D warnings → 0 warnings
    #   - tests >= 10 (config, serde, construction, API URL building)
    #   - Authorization header: "Bot {token}"
    #   - Debug output: token redacted

    # failure_strategy:
    #   - compile error → AI_fix_compile_error(error_msg, max_retry=3)
    #   - clippy warning → AI_fix_clippy_warning(warning_msg)

    # return:
    #   AdapterResult = {files_created, files_modified, test_count, summary}
```

**차이**: 자연어 17줄 → PG 35줄이지만, **의도 명확성, 검증 가능성, 실패 대응**이 구조적으로 내장됨.

---

## 8. 적용 규칙

### PGF execute 단계에서의 적용

1. **`[parallel]` 블록의 노드를 에이전트로 파견할 때**: 자연어 대신 PG TaskSpec 사용
2. **단일 노드를 에이전트로 파견할 때** (규모가 커서): PG TaskSpec 사용
3. **직접 실행할 때** (규모가 작아서): TaskSpec 불필요, PPR def 직접 해석/실행

### PG TaskSpec 사용 판단 기준

| 상황 | TaskSpec 사용 |
|---|---|
| 직접 실행 (15분 이내) | ❌ 직접 실행 |
| 에이전트 파견 (단순 작업) | ○ 간략 TaskSpec |
| 에이전트 파견 (복잡 작업) | ✅ 완전 TaskSpec |
| 다중 에이전트 병렬 파견 | ✅ 필수 — 결과 통합에 타입 계약 필요 |

### 에이전트 수행 후 결과 통합

```python
def integrate_agent_results(results: list[TaskResult]) -> IntegrationResult:
    """병렬 에이전트 결과 통합 + 교차 검증"""

    # 1. 모든 에이전트가 acceptance_criteria 충족 확인
    failed = [r for r in results if r["status"] != "done"]
    if failed:
        AI_handle_failures(failed)

    # 2. workspace 전체 통합 검증
    cargo_check("--workspace")

    # 3. 상호 의존성 검증 (에이전트 A의 결과가 에이전트 B에 영향?)
    AI_verify_cross_dependencies(results)
```
