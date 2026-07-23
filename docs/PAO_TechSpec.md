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
3. run `response --resident`, which publishes the identity-bound `starting`
   heartbeat and enters ADP in the same Python process
4. replace `starting` with the first operational heartbeat without returning to
   the agent; this closes the bounded startup phase independent of agent latency
5. run the watcher in resident mode and poll the mailbox every five seconds
6. cross each 90-second idle slice boundary inside the same watcher process,
   continuously refreshing heartbeat without returning to the agent
7. return to the agent only for a task, control event, or fatal watcher error
8. when a task appears, atomically move it `incoming -> claimed` and emit it on stdout
9. execute the task inside the same LWAR session
10. atomically write the result to `outgoing`
11. re-run the watcher
12. stop on resumable `shutdown`, successful clean `retire`, fatal ADP error, or the documented context-exhaustion handoff

OA status separates `registered_not_started`, `starting`, `active`, and `stale`.
Only current-identity `watching`, `idle`, and `running` heartbeats are eligible
for automatic routing; therefore an old-generation or startup marker cannot
receive work accidentally.

OA liveness is carried by `var/oa/presence.json`, refreshed on a monotonic
fixed-rate 25-second target with a 30-second hard latest and a 90-second expiry.
Deadlines advance from the prior deadline rather than command completion, so
foreground work does not accumulate cadence drift. The writer lease remains a 900-second single-writer
fence and must not be interpreted as process liveness. LWAR `oa-status`
classifies the signal as `live`, `stale`, `missing`, or `invalid`.

OA mutations are additionally serialized by `var/oa/.command.lock`. The writer
lease fences different OA identities; the command mutex prevents two processes
using the same identity from overlapping stale reads and writes. A contender
waits at most 30 seconds and then fails closed, while the active command keeps
presence and the writer lease renewed.

The command lock records its owner PID. POSIX and Windows liveness probes
prevent an old but live lock from being stolen. If the owner process terminates,
the orphaned lock becomes reclaimable after the 30-second stale threshold; this
restores mutation availability without operator file deletion.

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

A current-identity `starting` heartbeat that exceeds the 30-second startup
deadline remains non-routable. OA may explicitly reclaim that orphan only with
`recover --reap-startup` and the exact `lwar_id + instance_id + generation`.
The registry-locked operation rechecks identity, heartbeat state/age, and the
absence of active mailbox work before removing the slot and writing a
generation-preserving tombstone. Fresh startup, an operational/stale watcher,
an identity mismatch, or queued/claimed/control/result work fails closed.

The two-file reap commit is ordered tombstone first, registry second. A process
crash between writes therefore retains an occupied slot plus its generation
tombstone rather than exposing an unfenced free alias. Once orphaned locks age
past the stale threshold, repeating the exact recovery tuple completes registry
removal with one version increment. A crash after both writes converges through
the matching-tombstone `already_reaped` path. That post-commit replay does not
rewrite either state file or increment the registry version; it supplies the
lost response and records the deadline/reap audit events.

Accepted startup-reap audit events carry deterministic idempotency keys built
from the current identity tuple and event name. Key lookup and append occur
under the audit lock against the active log, rotated segments, and degraded
backlog. A crash between the deadline and reap audit appends can therefore be
retried without duplicating the first event while still restoring the second.
Fallback writes serialize under a separate degraded-spool lock and check that
backlog before appending. Repeated active-log failures therefore retain one
pending line per deterministic key, which the next healthy record promotes
once into the active stream. Promotion also filters backlog keys already found
in active or rotated segments. A process stop after active flush but before
spool deletion therefore converges on retry without duplicating keyed events.
Both active and degraded append paths cross an `fsync` durability barrier after
flush. The runtime deletes the spool only after active `fsync` succeeds; an
active durability failure falls back to an independently `fsync`-committed
spool entry.
Rotated-segment pruning acquires `.audit.lock` and then `.degraded.lock`, the
same order as append/replay. Before deleting an eligible old segment it checks
pending degraded keys; an intersecting segment is retained until replay clears
the spool. Unreadable pending or segment evidence fails closed.
Keyed append/replay also requires a complete readable key snapshot across all
active and rotated segments. A file read or UTF-8 decode failure raises the
audit operation into its non-fatal degraded path rather than treating unknown
evidence as key absence and risking a duplicate append.
Audit JSONL readers require every non-empty line to decode as a JSON object, so
valid unkeyed events remain distinct from unknown malformed evidence. Recovery
is deliberately bounded: only a malformed final fragment without a terminating
newline in mutable `events.jsonl` or `degraded.jsonl` is treated as a crash tail.
Its raw bytes are written and `fsync`-committed under `var/audit/.corrupt/`
before truncating and `fsync`-committing the source. Malformed rotated,
newline-terminated, or interior records stay unchanged and block keyed append
until explicit operator repair.

The read-only `oa audit-health` command scans the same JSONL contract without
locks or repair side effects. Its JSON response reports overall
`healthy|attention|blocked`, `keyed_append_blocked`, `blocked_replay`, each
segment's line/key/malformed state and SHA-256, exact `repair_candidates`,
degraded pending count, quarantine files, and safe remediation guidance.
Blocked health returns exit code 2; diagnostics never create OA lease/presence
or audit state.

Non-automatic corruption is repaired only through the writer-guarded `oa
audit-repair` command. The operator supplies a top-level audit segment name,
the SHA-256 of the diagnosed bytes, and exactly every malformed 1-based line.
The runtime rejects traversal, fingerprint drift, partial selection, and valid
line deletion. It holds `.audit.lock` then `.degraded.lock`, durably preserves
the original under `.corrupt/`, validates the complete candidate, and performs
an atomic replacement. A deterministic repair audit then drives degraded replay;
other corrupt segments remain fail-closed and cause exit code 2.

Repair intent is crash-durable. Before target replacement, the runtime writes a
receipt under `var/audit/.repairs/` binding the segment, original and repaired
SHA-256 values, exact dropped lines, sizes, and preserved backup. The receipt
advances `prepared -> replaced -> committed`. An exact retry with a `prepared`
receipt may replace an unchanged original or recognize the already-repaired
digest after a post-replace crash. A `replaced` receipt resumes the deterministic
audit append, whose idempotency key prevents duplication; a `committed` receipt
is a stable replay. Any other receipt/target combination fails closed.
`audit-health` surfaces receipt validity, phase, backup presence, and the pending
repair count without mutating recovery state.

Repair-evidence retention is committed-only and fail-closed. Under the audit and
degraded locks, `prune` requires an old parseable `committed_at`, the
deterministic repair audit key in a complete readable audit snapshot, a matching
original backup, and a target that proves the repaired content. For the active
log, later valid append-only records may follow the repaired prefix; for the
degraded spool, healthy replay may consume the file. The receipt is removed
before its backup, so a stop can leak evidence but cannot erase an unbound
backup. Any incomplete, invalid, drifted, missing, or ambiguous evidence remains
untouched.

Deletion is a resumable file transaction. Before removing evidence, the runtime
writes `var/audit/.repair-prune/<segment>.<original-sha256>.json`, binding the
receipt byte fingerprint, original and repaired hashes and sizes, deterministic
audit key, source backup, and deterministic staging path. Its initial
`authorized` phase precedes receipt removal. The original backup then moves by
atomic same-filesystem replacement into the staging path; the tombstone advances
durably to `backup_staged` before the staged bytes are removed. The tombstone is
removed last. On the next `prune`, file presence plus the strict tombstone,
audit-key, target, receipt, and backup checks identify the last safe transition.
An already-authorized cleanup completes independently of the new cutoff.
Malformed or conflicting state remains fail-closed.

Retention diagnosis is read-only. `audit-health` builds a non-repairing audit
key snapshot and validates every `.repair-prune/*.json` marker, repaired target,
receipt fingerprint, and original or staged backup. The five legitimate
topologies across `authorized` and `backup_staged` are reported as `resumable`.
Every other topology or validation failure is `blocked` with stable
`reason_codes`. The report exposes `retention_tombstones`,
`resumable_retention_count`, and `blocked_retention_count`. Either nonzero count
sets overall health to `attention`; it does not set `keyed_append_blocked` or
`blocked_replay`, whose meanings remain limited to audit-log safety.

Rotation retention is transaction-aware. Under the same audit/degraded lock
order, rotated pruning strictly snapshots every `.repair-prune/*.json` marker
before deleting any segment. It protects the marker's named target when that
target is `events.<digits>.jsonl`, plus every rotated segment whose parsed keys
contain the marker's deterministic repair audit key. Protection applies to
resumable and blocked transactions. If a marker is malformed, unreadable, or
not a regular file, no rotated segment is removed. Once retention completion
removes the marker, both fences disappear and normal age-based pruning resumes.

All remaining age-eligible rotated candidates pass a complete pre-delete
validation. The prune classifier accepts only readable UTF-8 JSONL whose every
line is a JSON object. Malformed, non-object, unreadable, metadata-inaccessible, or
unlink-failed candidates remain present and increment
`audit_segments_blocked`. Reference-fenced candidates increment
`audit_segments_protected`; successful deletions increment
`audit_segments_removed`. OA emits all three counts, while `total` includes
only successful deletion.

`audit_segment_outcomes` is the normalized evidence behind those aggregates.
Each item has a bus-root-relative `path`, one
`removed|protected|blocked` status, and stable `reason_codes`. Removal uses
`valid_expired`; protection distinguishes `retention_target`,
`retention_audit_key`, and `degraded_replay_key`; blocked outcomes distinguish
invalid retention/degraded snapshots, file read and JSONL validation failures,
metadata failures, disappearance, and unlink failure. Optional `error` text is
diagnostic only. The invariant is:

```text
len(audit_segment_outcomes)
  == audit_segments_removed
   + audit_segments_protected
   + audit_segments_blocked
```

OA includes the identical ordered list in command output and the `pruned` audit
event.

Rotated deletion uses a write-ahead run receipt under
`var/audit/.rotated-prune/<run_id>.json`. The receipt is atomically durable
before the first deletion and binds the original cutoff, path-sorted decisions,
and SHA-256 plus byte count for every `valid_expired` removal. Only one pending
receipt is allowed. A later invocation resumes it before taking a new candidate
snapshot, even when the new CLI-derived cutoff differs.

The apply phase rechecks each present authorized target against its witness.
Mismatch changes the outcome to reason-coded `segment_drifted` and retains the
file. Absence is accepted because deletion was already durably authorized; this
is the proof that lets a run converge after a process stop between unlink and
receipt advancement. Once every decision is terminal, the receipt advances
from `prepared` to `applied`.

OA writes `pruned` with deterministic idempotency key
`rotated-prune:<run_id>`. `commit_rotated_prune_receipt` reloads and matches the
public report, requires the receipt to be `applied`, and scans the complete
audit log for that key before removing the receipt. A degraded append therefore
returns `audit_prune_audit_committed=false` and retains the receipt. Retry
reconstructs the same outcomes, promotes or deduplicates the event exactly
once, and only then completes the receipt.

`audit-health` snapshots `.rotated-prune/` without acquiring locks or changing
filesystem state. `rotated_prune_receipts` exposes each path, phase, run ID,
cutoff, audit-key presence, outcome count, deletion-target states, and stable
`reason_codes`. Valid `prepared` targets may be fingerprint-matching or
authorized-absent; valid `applied` removal targets must be absent. These states
are `resumable`.

The classifier marks invalid schema, a non-directory receipt root, unexpected
or multiple entries, unreadable/non-file targets, fingerprint drift,
incomplete audit snapshots, and any `applied` deletion target that is present
as `blocked`. `resumable_rotated_prune_count` and
`blocked_rotated_prune_count` raise overall health to `attention`; only audit
segment validity controls `keyed_append_blocked`.

`audit-prune-resolve` is the guarded transition for
`applied_target_present`. Its operator fence is the tuple `(run_id,
receipt_sha256, segment, segment_sha256, decision=preserve-recreated)`. Under
the audit/degraded lock order, the runtime requires one exact applied receipt
and one regular target matching that tuple.

Before receipt mutation it writes
`var/audit/.rotated-preserve/<run_id>.<segment>.json`, binding the recreated
bytes, original deletion witness, receipt fingerprint, operator decision, and
resolution audit key. The receipt outcome changes from authorized removal to
retained `operator_preserved_recreated_segment`. A retry after marker-first or
receipt-first interruption reuses the same binding.

OA recovers or deduplicates the original `pruned` event under
`rotated-prune:<run_id>`, then appends `audit_prune_resolved` under
`rotated-prune-resolve:<run_id>:<segment>:<preserved_sha256>`. Receipt removal
requires both keys, the exact marker, and unchanged preserved target. Normal
rotated pruning strictly loads every preservation marker and classifies its
target as protected `operator_preserved_target`; any invalid marker snapshot
blocks the rotated pass as `preservation_snapshot_invalid`.

The read-only health classifier independently snapshots
`var/audit/.rotated-preserve/`. It emits `rotated_preservations`,
`protected_rotated_preservation_count`, and
`blocked_rotated_preservation_count`. A marker is `protected` only if its
strict schema/filename is valid, its regular target matches the recorded
SHA-256 and byte count, and both `rotated-prune:<run_id>` and its deterministic
resolution key exist in a complete audit-key snapshot. Stable blocked reasons
include invalid or unexpected entries, an orphaned marker, unreadable or
non-file target, fingerprint drift, duplicate claims on one target, incomplete
audit visibility, and missing original-prune or resolution keys. Inspection
acquires no lock and changes no file. Protected or blocked markers raise
overall health to `attention`; audit segment validity alone controls
`keyed_append_blocked`.

`audit-preserve-release` is the guarded retirement transition for one valid
permanent protection. Its operator fence is `(run_id, segment, marker_sha256,
segment_sha256, decision=release-protection)`. Preparation requires the strict
marker file, a matching regular target, both the original prune and resolution
audit keys, and no duplicate valid marker claim on the segment.

The deterministic release key is
`rotated-preserve-release:<run_id>:<segment>:<marker_sha256>:<segment_sha256>`.
OA records `audit_preservation_released` with the complete binding before
marker deletion. Commit reloads the exact event payload, marker bytes, target
bytes, prior audit keys, and health classification under the audit/degraded
lock order, then unlinks only the marker. An append failure leaves protection
active. A process stop after event commit resumes marker deletion; a stop after
unlink is recognized from the exact committed event. Payload collision or
duplicate event evidence fails closed. The segment is not mutated or deleted
by this transition and is eligible for a later normal rotated prune.

Read-only release diagnosis scans committed `events*.jsonl` bytes without
locks, tail repair, or mutation, selecting both canonical
`audit_preservation_released` events and every event occupying the
`rotated-preserve-release:` key namespace. Evidence is grouped by exact key.
The classifier validates the key-derived run, segment, marker hash, target
hash, prior audit keys, canonical event payload, and nonnegative target byte
count.

One valid event with an absent marker is `completed`. One valid event with the
exact marker still present and health-classified `protected` is `resumable`
with `release_event_committed_marker_present`. Duplicate events,
key/payload disagreement, marker fingerprint drift, non-file/unreadable
markers, and missing or blocked marker bindings are stable `blocked` states.
`preservation_releases` exposes event locations and the complete retry fence;
aggregate completed/resumable/blocked counts support automation. Completed
history is informational. Resumable or blocked evidence raises overall health
to `attention`, while JSONL validity alone controls `keyed_append_blocked`.

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
