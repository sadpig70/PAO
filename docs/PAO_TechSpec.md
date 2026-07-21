# PAO Technical Specification

## System Summary

| Item | Value |
|------|-------|
| System name | PAO |
| Expanded name | Poly Agents Orchestra |
| System type | heterogeneous CLI agent runtime orchestration system |
| Current transport | local file message bus |
| Future transport options | MCP, local IPC, SQLite queue, Redis, or equivalent |
| Default environment | local workspace |
| Document version | 0.2 — ADP architecture |
| Date | 2026-07-14 |

## 1. Overview

**PAO (Poly Agents Orchestra)** coordinates AI agent runtimes that differ by vendor, model, CLI, TUI, or headless execution style under one orchestration layer.

Its goal is not simple parallel prompting. The top-level orchestrator analyzes the user goal, decomposes work, selects a suitable runtime for each task, issues instructions, validates returned results, integrates partial outputs, and retries or reassigns work when needed.

The first implementation uses the local filesystem as the message transport layer. OA and runtime workers exchange tasks and results through agreed directories, filenames, and JSON contracts.

In PAO 0.2:

- the top-level orchestrator is **OA (Orchestration Agent)**
- each execution runtime is an **LWAR**
- the resident self-watch loop inside an LWAR session is **ADP (Agent Daemon Process)**

OA does not drive LWARs through non-interactive launch commands. Instead, the user starts long-running LWAR sessions, and each session repeatedly invokes a Python watcher to read its mailbox.

The file bus is the initial transport, not the system identity. The orchestration contract should survive replacement of the transport layer.

## 2. Identity

Recommended English description:

> **PAO is a local workspace-centered orchestration system for heterogeneous CLI agent runtimes, using a replaceable message transport layer.**

PAO is not limited to:

- one vendor's multi-agent feature
- a prompt fan-out tool
- a file synchronization utility
- a single CLI session manager
- file transport as a permanent design decision
- homogeneous runtimes using the same model everywhere

## 3. Goals

### Primary goal

Run diverse execution environments such as Claude Code CLI, Codex CLI, Grok-family runtimes, DeepSeek TUI, Kimi Code CLI, Qwen CLI, and OpenCode under one shared task contract.

### Secondary goal

Build an automated collaboration pipeline where the orchestrator can:

1. interpret the goal
2. decompose the work
3. select a runtime
4. assign role and constraints
5. deliver the task
6. collect the result
7. validate the result
8. recover or reassign on failure
9. integrate partial outputs
10. report the final outcome and evidence

### Design goals

- minimal functionality must work on a single machine and local filesystem
- no central broker is required for the MVP
- runtime-specific differences belong in adapter layers
- repeated short execution slices are preferred over monolithic sessions
- tasks and results use vendor-neutral contracts
- changing the transport layer must not rewrite core orchestration logic
- the system must detect and recover from failure, timeout, duplicate execution, and worker interruption

## 4. Core Design Principles

### 4.1 Separate orchestration from transport

PAO separates:

- **orchestration plane**: goal decomposition, runtime selection, validation, replanning, integration
- **transport plane**: task, state, result, and control delivery
- **execution plane**: actual reasoning, file edits, command execution, and artifact creation
- **state plane**: task lifecycle, worker state, retries, and history

The filesystem covers transport and part of state in the MVP, but the orchestration contract remains stable even if the transport changes later.

### 4.2 Shared task contract

All runtimes expose the same task and result contract externally, even when their internal interfaces differ. Runtime-specific options are isolated into adapter-specific fields.

### 4.3 Continuity through short executions

Workers may stay resident, but the actual AI runtime is invoked only when work arrives. Long tasks are handled as repeated short slices rather than one uncontrolled permanent execution.

### 4.4 No success without verification

A runtime exiting cleanly is not enough. Required artifacts, tests, evidence, format, and quality conditions must all be validated.

### 4.5 Runtime replaceability

If one runtime fails or no longer matches cost, speed, or quality needs, the same task should be transferable to a different runtime. Task prompts should therefore remain as vendor-neutral as possible.

## 5. Scope

### Included in scope

- multi-step work planning and task graph construction
- per-runtime role assignment
- message delivery and result collection
- local CLI, TUI, and headless runtime execution
- status, heartbeat, lock, and log management
- result normalization
- verification and cross-review
- timeout handling, retries, and alternate-runtime reassignment
- artifact tracking for files, patches, and reports
- future routing extensions based on cost, latency, and success rate

### Initially out of scope

- millisecond-grade real-time messaging
- global distributed transactions at large scale
- training or fine-tuning foundation models
- replacing vendor authentication or billing systems
- fully general secure sandboxing for arbitrary untrusted code
- guaranteed automation for every TUI program

## 6. High-Level Architecture

PAO is organized into six logical layers:

1. **Goal and routing layer**: analyze goals, decompose tasks, choose runtimes, replan when needed
2. **Validation and integration layer**: verify outputs, cross-check results, integrate final artifacts
3. **Task state layer**: manage task DAG, lifecycle, retries, and execution history
4. **Transport layer**: move task, result, and control payloads between OA and LWARs
5. **Adapter and execution layer**: translate common tasks into runtime-specific behavior
6. **Runtime layer**: the actual AI runtimes that reason and act

## 7. Transport Abstraction

The transport layer must support at least:

- publish task
- claim task
- publish result
- publish and read control messages
- heartbeat updates
- lease tracking
- task recovery

Implementations may use files, MCP, databases, or brokers as long as they preserve these semantics.

Since 0.3, these semantics are codified as the `Transport` protocol in `pao_runtime/transport.py`; `FileTransport` is its filesystem implementation, and orchestration code (`oa_cli`, `lwar_cli`, `adp_watch`) references only the protocol surface.

### Initial `FileTransport`

The first implementation uses the local filesystem because it is simple to build, easy to debug, human-inspectable, serverless, and sufficient for a single workstation.

### Future `MCPTransport`

When PAO moves to MCP, task and result schemas stay the same. Only transport delivery and tool binding change.

## 8. MVP Directory Layout

```text
mailbox/
  LWAR1/
    incoming/
    claimed/
    outgoing/
    control/
    control_claimed/
    leases/
    work/
    archive/
    failed/
    dead/
    quarantine/
    heartbeat.json
var/
  registry/
  identities/
  tasks/          # OA task ledger, per workflow
  audit/          # append-only events.jsonl
```

## 9. File Rules

| Path | Meaning | Writer | Reader |
|------|---------|--------|--------|
| `inbox/{runtime}.json` | latest instruction to that runtime | orchestrator | worker |
| `outbox/{runtime}.json` | latest result from that runtime | worker | orchestrator |
| `heartbeat/{runtime}.json` | worker liveness and availability | worker | orchestrator |
| `lock/{runtime}.lock` | duplicate-execution guard | worker | worker and orchestrator |
| `tasks/*/{task_id}.json` | task contract and state | both sides | both sides |
| `results/{task_id}.json` | normalized execution result | worker | orchestrator and validators |
| `artifacts/{task_id}/` | patches, reports, and output files | worker | every layer |
| `logs/{runtime}.log` | stdout, stderr, and audit log | worker | operator and orchestrator |

Treat path and filename conventions as part of the transport protocol and version them accordingly.

## 10. Task Schema

Example:

```json
{
  "task_id": "task-001",
  "schema_version": "0.2",
  "goal": "Analyze and fix the failing tests in the target module.",
  "instructions": "Inspect tests/test_auth.py, identify the failure cause, fix it, then validate the result.",
  "completion_criteria": [
    "All related tests pass",
    "Changed files and reasons are reported",
    "Unresolved risks are reported"
  ],
  "expected_output": "Patch, test results, change summary, follow-up recommendation"
}
```

Rules:

- `task_id` must be globally unique
- `schema_version` controls compatibility
- `completion_criteria` should be verifiable
- runtime options stay inside adapter-specific fields
- working directory and permissions must be explicit
- prompt body and execution policy must remain separate
- never put secrets or tokens into the task payload

## 11. Result Schema

Example:

```json
{
  "status": "succeeded",
  "summary": "The auth token expiry branch was fixed and 12 tests passed.",
  "evidence": {
    "commands": ["python -m unittest ..."],
    "tests_passed": 12,
    "tests_failed": 0
  }
}
```

Raw stdout or logs may be preserved elsewhere, but OA consumes a normalized result contract.

## 12. ADP-Based LWAR Execution

Each LWAR is started once by the user in an interactive or TUI session. The
runtime is told only to read `pao-lwar/SKILL.md` and act as a PAO LWAR; that
skill autonomously bootstraps registration and keeps ADP alive inside the
session. The Python watcher handles deterministic message I/O; the LWAR agent
remains responsible for repetition and task execution.

### Basic loop

1. start the LWAR session, inspect OA presence, and self-register (OA may start later)
2. receive OA approval for the `LWARn` identity
3. run the ADP watcher
4. poll the mailbox every five seconds
5. if no message arrives, emit `idle_timeout` after 90 seconds and exit
6. immediately run the watcher again
7. when a task appears, atomically move it `incoming -> claimed` and emit it on stdout
8. execute the task inside the same LWAR session
9. atomically write the result to `outgoing`
10. re-run the watcher
11. stop on resumable `shutdown`, successful clean `retire`, fatal ADP error, or the documented context-exhaustion handoff

OA liveness is carried by `var/oa/presence.json`, refreshed every 30 seconds
with a 90-second expiry. The writer lease remains a 900-second single-writer
fence and must not be interpreted as process liveness. LWAR `oa-status`
classifies the signal as `live`, `stale`, `missing`, or `invalid`.

Clean retirement is an idempotent lifecycle pipeline:
`control:retire -> on -> draining -> off -> deregistered`. Each transition is
approved by OA `reconcile`; ADP stops only after `lwar_retired` confirms the
registry slot is absent. `shutdown` remains a distinct resumable stop that keeps
the slot allocated.

## 13. Failure Recovery

PAO must recover from:

- watcher exit without session death
- session death with stale heartbeat
- expired leases on claimed tasks
- duplicate or replayed result submission
- stale identities after slot reuse

OA recovery returns expired claimed tasks to the queue and never trusts stale generation output as current work.

Concretely, since 0.3:

- each requeue increments `attempt`; exceeding `max_retries` dead-letters the task into `dead/` (manual `dead --requeue` resets the budget)
- `collect` quarantines stale-generation and duplicate results instead of accepting them
- claim leases are aligned with the task's `timeout_s` so long tasks cannot expire mid-execution
- every published task is tracked in the OA ledger (`var/tasks/`), giving `validate` and `workflow-status` a durable state source

## 14. Security and Control

Every task must declare:

- allowed read/write scope
- network allowance
- timeout
- working directory

LWARs must not exceed the declared authority. OA must verify actual behavior through evidence collection.

## 15. Deployment Modes

The runtime is workspace-independent. Distribution is a single skills-only
channel: `.agents/skills/pao-oa` and `.agents/skills/pao-lwar` are self-contained skills
bundling the wrapper scripts and runtime (plus message schemas for the LWAR),
installed by copying each folder into any global skills directory (`~/.claude/skills`,
`~/.agents/skills`, or any path the runtime loads) — no installer required. Only
the bus root is needed (`--root` > `PAO_ROOT` > the `.pao/` default under cwd), and
commands resolve paths through the `<PAO_SKILL>` placeholder (the folder containing
the loaded SKILL.md). `.agents/skills/pao-lwar` is the runtime master; `tools/sync_bundles.py`
mirrors it into `pao-oa`, and the test suite byte-verifies the two bundles match.
The channel is vendor-neutral — proven on Claude Code and Kimi Code CLI. (A Claude
Code plugin channel existed through 0.6.2 and was retired to `_legacy/` in favor of
this single portable channel.)

The two role skills are also the sole operating prompts. A runtime given only
"read `pao-oa/SKILL.md` and act as OA" or "read `pao-lwar/SKILL.md` and act as
LWAR" must execute the role bootstrap without a second repository guide or
vendor-specific prompt. Repository documentation may point to those skills but
must not duplicate their mutable command contracts.

- the bus root resolves as explicit `--root` > `PAO_ROOT` environment variable > `<cwd>/.pao`
- each copied skill's `scripts/*.py` wrappers work from any directory and bootstrap their bundled runtime without installation
- the bundled `pao.py install-skills` command is only a convenience for copying the same two canonical skill folders; it does not create another distribution channel
- the bus stays central (one registry, one `LWARn` identity space per machine) while each task executes in its own `cwd`, enabling cross-project orchestration
- `send` rejects tasks whose `cwd` does not exist, failing fast on stale workspace paths

## 16. Evolution Path

Planned future steps include:

- alternative transports such as MCP or SQLite-backed queues
- richer runtime capability models
- routing informed by empirical success rate and cost
- stronger validation pipelines
- higher-level delegation policies across multiple LWAR classes

The core invariant remains unchanged: PAO is an orchestration contract first, and a transport choice second.
