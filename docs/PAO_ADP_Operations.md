# PAO ADP Operations Guide

## Purpose

This document describes how an LWAR session keeps running as an ADP-driven worker, how OA interacts with it, and what operators should expect during normal and failure conditions.

## Runtime Model

- the user starts an LWAR session manually
- the session registers and receives an approved identity
- the session repeatedly invokes `python scripts/adp_watch.py`
- the watcher performs deterministic mailbox I/O and exits with an event
- the LWAR agent handles the event, executes tasks when required, submits results, and calls the watcher again

This model supports runtimes that do not expose a reliable non-interactive command mode.

## Standard Loop

```text
register -> approved identity -> watch
watch idle/state_wait -> watch again
watch task_received -> execute -> submit result -> watch again
watch control -> handle -> watch again
watch shutdown -> stop
```

## Event Semantics

| Event | Meaning | Required reaction |
|------|---------|-------------------|
| `idle_timeout` | no message arrived during the watch slice | immediately re-run the watcher |
| `state_wait` | lifecycle state does not allow work | re-run the watcher without executing tasks |
| `task_received` | a task was atomically claimed | execute it and submit a result |
| `control` | OA issued a command | handle it according to the command |
| `adp_error` | watcher-level error | report and stop |

## Control Commands

| Command | Meaning | LWAR behavior |
|---------|---------|---------------|
| `ping` | health probe | continue watching |
| `drain` | finish current work, then stop accepting new work | request lifecycle `draining` after current task |
| `cancel` | stop one task | submit a cancelled result for that task |
| `shutdown` | terminate ADP | stop the loop |

## Result Submission

Every claimed task must end with one normalized result:

- `succeeded`
- `failed`
- `blocked`
- `cancelled`

The result must include:

- summary
- evidence
- exit code
- artifacts if any
- error details if applicable

## Failure Cases

### Watcher exits, session survives

The session simply invokes the watcher again.

### Session dies

Heartbeat becomes stale. OA eventually detects the failure and may recover claimed tasks after lease expiry.

### Lease expires

OA moves the task back to `incoming` so another valid runtime instance can claim it. Each requeue increments the task's `attempt`; when `attempt` exceeds `max_retries`, the task is moved to `dead/` with an error record instead of being requeued. Dead tasks are listed with `oa_cli dead` and can be republished with `oa_cli dead --requeue TASK_ID` (attempt resets to 1).

The claim lease is aligned with the task budget at claim time: `effective_lease_s = max(--lease-seconds, timeout_s + 30)`, so a long task does not lose its lease mid-execution.

### Slot is reused

`generation` and `instance_id` prevent stale messages or results from being accepted as current work.

### Stale or duplicate results

`oa_cli collect` verifies every result's `(instance_id, generation)` tuple against the current registry and its task against the ledger. Stale-generation results and replays of already-collected tasks are moved to `quarantine/` with an error record and are never included in the collected set.

## Task Ledger and Validation

OA keeps a durable ledger entry per published task at `var/tasks/{workflow_id}/{task_id}.json`, transitioning through `published -> requeued* -> completed | dead`. Supporting commands:

- `oa_cli validate --task-id TASK_ID` — mechanical checks (result status, exit code consistency, evidence presence) plus the completion-criteria checklist for OA review
- `oa_cli workflow-status --workflow-id ID` — per-workflow aggregation of ledger statuses
- task drafts may declare `depends_on`; publication is blocked until every dependency completed with a succeeded result

## Automatic Routing

`oa_cli send --auto --require-capability X` selects the target LWAR automatically: only `on` slots holding every required capability are eligible, scored by incoming backlog plus busy/stale penalties, with deterministic ties toward the lowest LWAR number. A stale heartbeat effectively excludes a candidate. No eligible LWAR is an explicit error.

## Maintenance

- `oa_cli prune --older-than-days N` removes old archive, `failed/`, and `quarantine/` files; `dead/` is never pruned automatically
- all OA, LWAR, and ADP actions are mirrored to the append-only audit log `var/audit/events.jsonl`

## Operator Guidance

- do not hand-edit mailbox state
- verify heartbeat and lifecycle before publishing work
- do not treat `exit_code=0` alone as task success
- inspect evidence and completion criteria before approval
- prefer generation-safe identity files over hard-coded slot assumptions
