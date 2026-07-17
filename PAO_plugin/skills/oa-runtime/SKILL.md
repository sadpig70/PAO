---
name: oa-runtime
description: "PAO Orchestration Agent contract for approving LWAR registrations, publishing mailbox tasks and controls, collecting results, monitoring ADP heartbeat, and recovering stale leases. Load whenever acting as OA or managing PAO LWARs."
user-invocable: true
argument-hint: "info | doctor | status | reconcile | send | collect | validate | workflow-status | recover | dead | control | prune"
---

# OA Runtime Skill v1.1

> OA is the **Orchestration Agent**. OA does not launch LWARs. It approves registrations, publishes mailbox tasks, and validates and integrates results. Long-running execution is owned by each LWAR's ADP. An **LWAR** (Long-running Worker Agent Runtime) is the stable execution identity (`LWAR1`, `LWAR2`, ...) that hides provider and model names.

## 0. Bus Root Resolution and Invocation

All commands resolve the bus root as: explicit `--root` > `PAO_ROOT` environment variable > current directory. In operation mode (any project workspace), set `PAO_ROOT` once and omit `--root`.

Every example below invokes the CLI as `python "$PAO_HOME/scripts/oa.py"`, where `$PAO_HOME` is the PAO installation directory. Resolve it in this order:

1. **Claude Code plugin install** — the installation directory is: `${CLAUDE_PLUGIN_ROOT}`. When this skill is loaded from the installed plugin, the token on the previous line is already substituted with the absolute plugin path; use that path in place of `$PAO_HOME`. If the token appears unsubstituted, fall back to the next rule.
2. **Manual / foreign-runtime install** — set the `PAO_HOME` environment variable to the PAO repository path.
3. **Inside the PAO repository** — `$PAO_HOME` is the repository's `PAO_plugin/` directory. `python -m pao_runtime.oa_cli` (from inside `PAO_plugin/`) and, after `pip install -e`, `pao-oa` remain equivalent alternatives here.

The wrapper scripts bootstrap their own import path; no pip install is required in any mode. Diagnose version and root resolution with `python "$PAO_HOME/scripts/pao.py" info`. Before the first orchestration action of a session, run the pre-flight check and stop on failure: `python "$PAO_HOME/scripts/pao.py" doctor --role oa`. The bus assumes a single-host local filesystem.

**Single-writer rule**: set a unique `PAO_OA_ID` environment variable once per OA session. Every mutating command (`reconcile`, `send`, `control`, `collect`, `recover`, `dead --requeue`, `validate --record`, `prune`) refreshes the writer lease at `var/oa/writer_lease.json` (TTL 900s); a session with a different id is rejected as a read-only observer until expiry. Sessions without `PAO_OA_ID` share the `oa-default` holder and get no mutual exclusion.

## 1. Core Loop

```text
OA // PAO supervising agent (in-progress)
    Reconcile // approve registration and lifecycle requests (in-progress)
    Plan // decompose goals into TaskContracts (in-progress)
    Publish // atomically publish to active LWAR mailboxes (in-progress) @dep:Plan
    Monitor // watch heartbeat, lease, and results (in-progress) @dep:Publish
    Validate // verify result evidence (in-progress) @dep:Monitor
    Recover // requeue, reassign, or dead-letter on failure (in-progress) @dep:Validate
```

## 2. Registration Approval

```bash
python "$PAO_HOME/scripts/oa.py" reconcile
python "$PAO_HOME/scripts/oa.py" status
```

`reconcile` processes requests against schema and identity rules, then atomically assigns the lowest available `LWARn`. Slots in `on`, `draining`, or `off` remain occupied.

## 3. Task Publication

Write a task draft first.

```json
{
  "goal": "Requested objective",
  "instructions": "Concrete instructions",
  "completion_criteria": ["Verification criteria"],
  "cwd": "workspace/project",
  "timeout_s": 90,
  "priority": 5,
  "permissions": {"read": [], "write": [], "network": false}
}
```

```bash
python "$PAO_HOME/scripts/oa.py" send --lwar-id LWAR1 --task-file TASK_DRAFT.json
python "$PAO_HOME/scripts/oa.py" send --auto --require-capability coding --task-file TASK_DRAFT.json
```

The OA tool binds `instance_id`, `generation`, and `registry_version` from the registry into the task. OA must not edit mailbox files directly.

**Authority bounds** (enforced): `send` rejects a `cwd` or `permissions.read`/`write` entry at-or-under the bus control surfaces (`mailbox/`, `var/`, `control/`) or the runtime bundle; the claim-side watcher re-checks the cwd deny-set. Omitted `permissions` defaults to `{"read": [cwd], "write": [cwd], "network": false}`; optional `permissions.max_artifact_bytes` caps each artifact.

- `--auto` routes by capability and load: only `on` LWARs holding every `--require-capability` are eligible; ties break toward the lowest backlog, then the lowest LWAR number. No eligible LWAR is an explicit error — never fall back to an arbitrary LWAR.
- A task draft may declare `depends_on: ["task-..."]`. Publication is blocked until every dependency is `completed` with a `succeeded` result in the task ledger.
- Every publication is recorded in the task ledger at `var/tasks/{workflow_id}/{task_id}.json`.

## 4. Monitoring and Collection

```bash
python "$PAO_HOME/scripts/oa.py" status
python "$PAO_HOME/scripts/oa.py" collect
python "$PAO_HOME/scripts/oa.py" collect --archive
python "$PAO_HOME/scripts/oa.py" recover
python "$PAO_HOME/scripts/oa.py" dead
python "$PAO_HOME/scripts/oa.py" dead --lwar-id LWAR1 --requeue TASK_ID
python "$PAO_HOME/scripts/oa.py" validate --task-id TASK_ID
python "$PAO_HOME/scripts/oa.py" workflow-status --workflow-id WORKFLOW_ID
```

- `status` computes heartbeat staleness (`heartbeat_stale`, default threshold 120s via `--stale-after`).
- `collect` quarantines stale-generation, stale-attempt, and duplicate results into `quarantine/` and marks accepted tasks `completed` in the ledger. Results echo the `attempt` of the claim they came from; a mismatch against the ledger (`stale_attempt_result`) means the result belongs to a superseded claim.
- A result is a **terminal submission, not a success claim** — only `status=succeeded` results are acceptance candidates; `failed`, `blocked`, `cancelled`, `interrupted`, `timed_out`, and `protocol_error` are collected for the record.
- `recover` increments `attempt` on each requeue and writes an `interruption` record (`recorded_by: oa_reconciler`) into the ledger entry — a vanished LWAR is recorded as interrupted, never inferred as success; when `attempt` exceeds `max_retries`, the task is dead-lettered into `dead/` instead of looping forever.
- `dead --requeue` republishes a dead task with `attempt` **incremented** (never reset — attempt is the collect-side fencing key and must stay monotonic).
- `collect` also verifies artifact provenance: artifact objects' content-addressed snapshots (`var/artifacts/<sha256>`) are size-checked then re-hashed; mismatches quarantine the result as `artifact_tampered`. Provenance detects post-submit mutation — it does not make artifacts trustworthy.
- `validate` reports mechanical checks (status, exit code, evidence presence, artifact verification) plus the `completion_criteria` checklist; semantic verification remains OA's judgment. `validate --record` persists the ValidationDecision into the ledger (mutating; takes the writer lease) — plain `validate` stays observer-safe.
- `recover` also reconciles rejected tasks parked in `failed/`: their non-terminal ledger entries transition to `failed` with the rejection reason.
- Never approve success from `exit_code=0` alone.
- Validate `completion_criteria`, evidence, artifacts, and actual test results.

## 5. Control

```bash
python "$PAO_HOME/scripts/oa.py" control --lwar-id LWAR1 --command ping
python "$PAO_HOME/scripts/oa.py" control --lwar-id LWAR1 --command drain
python "$PAO_HOME/scripts/oa.py" control --lwar-id LWAR1 --command cancel --task-id TASK_ID
python "$PAO_HOME/scripts/oa.py" control --lwar-id LWAR1 --command shutdown
```

`shutdown` requests ADP termination only. Deregistration is handled separately through lifecycle requests and `reconcile`.

## 6. Maintenance

```bash
python "$PAO_HOME/scripts/oa.py" prune --older-than-days 14
```

- `prune` removes archived tasks/results/control, `failed/`, and `quarantine/` files older than the cutoff. `dead/` is never pruned automatically — dead tasks require an explicit decision (`dead --requeue` or manual removal).
- Every OA, LWAR, and ADP action is mirrored to the append-only audit log at `var/audit/events.jsonl`.

## 7. Forbidden Actions

- Do not inject tasks by directly driving a vendor CLI or TUI.
- Do not expose provider names in external mailbox paths.
- Do not publish new tasks to an `off` or `draining` LWAR.
- Do not approve results from a stale identity as current-generation output.
- Do not rewrite failed validation as success.
- Do not edit mailbox, registry, or lease files by hand; act only through the bundled CLI.
