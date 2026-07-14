# LWAR ADP Bootstrap Guide

## Purpose

This guide provides the bootstrap prompt and operating contract for an LWAR session that will register itself, adopt an approved identity, and remain in the ADP watch/execute loop.

## Bootstrap Summary

An LWAR session should:

1. load the `lwar-runtime` skill
2. request registration, optionally with a specific slot number
3. wait for OA approval
4. adopt the returned identity file
5. run the watcher
6. react only to watcher events
7. keep looping until `shutdown`

## Registration

Automatic slot:

```bash
python scripts/lwar.py register \
  --runtime-name "Codex" \
  --model "GPT 5.5 Sol" \
  --adapter-id codex \
  --vendor-family openai \
  --interface cli \
  --root .
```

Specific slot:

```bash
python scripts/lwar.py register 1 \
  --runtime-name "Codex" \
  --model "GPT 5.5 Sol" \
  --adapter-id codex \
  --vendor-family openai \
  --interface cli \
  --root .
```

## Identity Adoption

After OA reconciliation:

```bash
python scripts/lwar.py response <request_id> --root .
```

Only when the event confirms identity adoption should the session continue. The returned identity file is the canonical identity handle for all later commands.

## Watch Loop

```bash
python scripts/adp_watch.py \
  --identity-file <identity_file> \
  --root . \
  --interval 1 \
  --timeout 90
```

Loop behavior:

- `idle_timeout` -> invoke the watcher again
- `state_wait` -> invoke the watcher again
- `task_received` -> perform the task, submit the result, invoke the watcher again
- `control` -> handle the command, then invoke the watcher again unless it is `shutdown`
- `shutdown` -> stop

## Result Completion

```bash
python scripts/lwar.py complete \
  --identity-file <identity_file> \
  --task-id <task_id> \
  --result-file mailbox/LWARn/work/<task_id>/result.json \
  --root .
```

## Lifecycle Changes

```bash
python scripts/lwar.py state draining --identity-file <identity_file> --root .
python scripts/lwar.py state off --identity-file <identity_file> --root .
python scripts/lwar.py state on --identity-file <identity_file> --root .
python scripts/lwar.py state deregistered --identity-file <identity_file> --root .
```

Use `deregistered` only after `off`.

## Prohibited Behavior

- do not self-assign `LWARn` before OA approval
- do not edit registry or mailbox files by hand
- do not accept stale identities
- do not abandon a claimed task without a result
- do not stop the loop without `shutdown` unless an unrecoverable watcher error occurs
