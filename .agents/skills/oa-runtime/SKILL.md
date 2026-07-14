---
name: oa-runtime
description: "PAO Orchestration Agent contract for approving LWAR registrations, publishing mailbox tasks and controls, collecting results, monitoring ADP heartbeat, and recovering stale leases. Load whenever acting as OA or managing PAO LWARs."
user-invocable: true
argument-hint: "reconcile | send | control | collect | recover | status"
---

# OA Runtime Skill v1

> OA is the **Orchestration Agent**. OA does not launch LWARs. It approves registrations, publishes mailbox tasks, and validates and integrates results. Long-running execution is owned by each LWAR's ADP.

## 0. Bus Root Resolution and Invocation

All commands resolve the bus root as: explicit `--root` > `PAO_ROOT` environment variable > current directory. In operation mode (any project workspace), set `PAO_ROOT` once and omit `--root`.

Invocation forms, all equivalent:

- `python "$PAO_HOME/scripts/oa.py" ...` — works from any directory with no installation (the wrapper bootstraps its import path)
- `python -m pao_runtime.oa_cli ...` — inside this repository, or anywhere after `pip install -e`
- `pao-oa ...` — optional console script from `pip install -e`

Diagnose version and root resolution with `python "$PAO_HOME/scripts/pao.py" info` (or `pao info`).

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
python -m pao_runtime.oa_cli reconcile
python -m pao_runtime.oa_cli status
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
python -m pao_runtime.oa_cli send --lwar-id LWAR1 --task-file TASK_DRAFT.json
python -m pao_runtime.oa_cli send --auto --require-capability coding --task-file TASK_DRAFT.json
```

The OA tool binds `instance_id`, `generation`, and `registry_version` from the registry into the task. OA must not edit mailbox files directly.

- `--auto` routes by capability and load: only `on` LWARs holding every `--require-capability` are eligible; ties break toward the lowest backlog, then the lowest LWAR number. No eligible LWAR is an explicit error — never fall back to an arbitrary LWAR.
- A task draft may declare `depends_on: ["task-..."]`. Publication is blocked until every dependency is `completed` with a `succeeded` result in the task ledger.
- Every publication is recorded in the task ledger at `var/tasks/{workflow_id}/{task_id}.json`.

## 4. Monitoring and Collection

```bash
python -m pao_runtime.oa_cli status
python -m pao_runtime.oa_cli collect
python -m pao_runtime.oa_cli collect --archive
python -m pao_runtime.oa_cli recover
python -m pao_runtime.oa_cli dead
python -m pao_runtime.oa_cli dead --lwar-id LWAR1 --requeue TASK_ID
python -m pao_runtime.oa_cli validate --task-id TASK_ID
python -m pao_runtime.oa_cli workflow-status --workflow-id WORKFLOW_ID
```

- `status` computes heartbeat staleness (`heartbeat_stale`, default threshold 120s via `--stale-after`).
- `collect` quarantines stale-generation and duplicate results into `quarantine/` and marks accepted tasks `completed` in the ledger.
- `recover` increments `attempt` on each requeue; when `attempt` exceeds `max_retries`, the task is dead-lettered into `dead/` instead of looping forever.
- `dead --requeue` republishes a dead task with `attempt` reset to 1.
- `validate` reports mechanical checks (status, exit code, evidence presence) plus the `completion_criteria` checklist; semantic verification remains OA's judgment.
- Never approve success from `exit_code=0` alone.
- Validate `completion_criteria`, evidence, artifacts, and actual test results.

## 5. Control

```bash
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command ping
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command drain
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command cancel --task-id TASK_ID
python -m pao_runtime.oa_cli control --lwar-id LWAR1 --command shutdown
```

`shutdown` requests ADP termination only. Deregistration is handled separately through lifecycle requests and `reconcile`.

## 6. Maintenance

```bash
python -m pao_runtime.oa_cli prune --older-than-days 14
```

- `prune` removes archived tasks/results/control, `failed/`, and `quarantine/` files older than the cutoff. `dead/` is never pruned automatically — dead tasks require an explicit decision (`dead --requeue` or manual removal).
- Every OA, LWAR, and ADP action is mirrored to the append-only audit log at `var/audit/events.jsonl`.

## 7. Forbidden Actions

- Do not inject tasks by directly driving a vendor CLI or TUI.
- Do not expose provider names in external mailbox paths.
- Do not publish new tasks to an `off` or `draining` LWAR.
- Do not approve results from a stale identity as current-generation output.
- Do not rewrite failed validation as success.
