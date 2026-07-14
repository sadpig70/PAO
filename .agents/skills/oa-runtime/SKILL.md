---
name: oa-runtime
description: "PAO Orchestration Agent contract for approving LWAR registrations, publishing mailbox tasks and controls, collecting results, monitoring ADP heartbeat, and recovering stale leases. Load whenever acting as OA or managing PAO LWARs."
user-invocable: true
argument-hint: "reconcile | send | control | collect | recover | status"
---

# OA Runtime Skill v1

> OA is the **Orchestration Agent**. OA does not launch LWARs. It approves registrations, publishes mailbox tasks, and validates and integrates results. Long-running execution is owned by each LWAR's ADP.

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
```

The OA tool binds `instance_id`, `generation`, and `registry_version` from the registry into the task. OA must not edit mailbox files directly.

## 4. Monitoring and Collection

```bash
python -m pao_runtime.oa_cli status
python -m pao_runtime.oa_cli collect
python -m pao_runtime.oa_cli collect --archive
python -m pao_runtime.oa_cli recover
```

- A stale heartbeat signals an LWAR failure.
- When a lease expires, `recover` returns the claimed task to `incoming`.
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

## 6. Forbidden Actions

- Do not inject tasks by directly driving a vendor CLI or TUI.
- Do not expose provider names in external mailbox paths.
- Do not publish new tasks to an `off` or `draining` LWAR.
- Do not approve results from a stale identity as current-generation output.
- Do not rewrite failed validation as success.
