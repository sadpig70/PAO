---
name: pao-lwar
description: "PAO LWAR (standalone, self-contained) — self-registration and the ADP (Agent Daemon Process) resident watch/execute loop. Bundles the PAO runtime; installs by folder copy alone — no pip, no plugin, no environment variable besides PAO_ROOT. Load on /pao-lwar or whenever this session must act as a PAO LWAR."
user-invocable: true
argument-hint: "info | doctor | register [number] | response | adp | status | on | drain | off | unregister"
---

# PAO-LWAR Skill v1.1 (standalone)

## Definitions

- **PAO** — Persistent Agent Orchestration: local orchestration of long-running AI runtimes over a file bus.
- **OA** — Orchestration Agent: approves registrations, publishes tasks, validates results.
- **LWAR** — Long-running Worker Agent Runtime: the stable execution identity (`LWAR1`, `LWAR2`, ...) that hides provider and model names.
- **ADP** — Agent Daemon Process: this already-running session repeatedly invokes a Python watcher, receives its mailbox, performs work, stores a result, and returns to the watcher. The watcher is a deterministic I/O tool; the repeating actor is this agent.
- **TaskContract / ResultContract** — the task and result JSON payloads; schemas live in [schemas/](schemas/).

## 0. Self-Contained Invocation

This skill bundles the full PAO runtime (`scripts/`, `pao_runtime/`, `schemas/`). In every command, replace the placeholder `<PAO_SKILL>` with the **absolute path of the folder containing this SKILL.md**. It is a documentation placeholder, not an environment variable — never pass it to a shell unresolved, and always quote the substituted path.

```bash
python "<PAO_SKILL>/scripts/lwar.py" register
```

Bus root resolution: explicit `--root` > `PAO_ROOT` environment variable > current directory. In operation mode set `PAO_ROOT` to the central bus and omit `--root`; task execution still happens in each task's own `cwd`. The bus assumes a **single-host local filesystem** (atomic rename semantics are not guaranteed on NFS/SMB shares). Run commands with the current runtime's Python executable — do not assume `python` and `python3` resolve to the same interpreter.

Before registering or starting ADP, run the pre-flight check and stop on failure:

```bash
python "<PAO_SKILL>/scripts/pao.py" doctor --role lwar
```

## 0.5 Session Bootstrap (cold start)

Run this decision flow at the start of a session, before any other action:

```text
1. doctor --role lwar   → unhealthy? stop and report.
2. Do you already hold an identity file from a prior session (var/identities/<instance>.json)?
   AND does `lwar.py status --identity-file <that file>` report your slot still present
   with a matching (lwar_id, instance_id, generation)?
     YES → RESUME: skip registration. If state is not `on`, request `state on`
            (lifecycle.md) and poll status until on; then go to ADP.
     NO  → REGISTER: run `lwar.py register …` (register.md), remember request_id,
            poll `lwar.py response REQUEST_ID` until `identity_adopted`, keep the
            printed identity_file (absolute) for every later call.
3. Enter the ADP loop (adp-loop.md) and keep it alive until `shutdown`.
```

Never re-register when a valid identity already exists — a needless re-registration
takes a new slot/generation and orphans the old one. All `--identity-file` and
`--result-file` arguments must be **absolute paths** (they resolve against the
process cwd, not the bus root).

## 1. Absolute Rules

1. Before registering, read [references/register.md](references/register.md). Before the **first** watch slice, read [references/adp-loop.md](references/adp-loop.md) **in full** — the exit-code contract, lease alignment, and stale-identity rejection are pre-loop knowledge, not lookup-on-event material. Read each reference in full once per session before its first use; re-read only if the file or the runtime version changes.
2. Use only the approved `(lwar_id, instance_id, generation)` as your runtime identity. Never claim an `LWARn` identity before approval.
3. Do not assume an external process will relaunch the LWAR. Keep ADP alive inside the current session.
4. On `idle_timeout` and `state_wait`, generate no extra explanation. Re-run the same watcher immediately.
5. On `task_received`, operate only within the TaskContract authority bounds and submit **exactly one terminal result** with `complete` before returning to the watcher, whenever this agent remains capable of submitting one. `complete` means terminal submission, **not success** — `failed`, `blocked`, `cancelled`, `timed_out`, and `protocol_error` outcomes are all submitted the same way. A crash or forced termination is recovered by lease expiry and OA `recover`; it is never inferred as success.
6. Only `shutdown` terminates ADP — with one exception: when session context exhaustion is imminent, execute the handoff procedure in [references/lifecycle.md](references/lifecycle.md) (request `state draining`, submit the terminal result for any claimed task, then request `state off` or re-register from a fresh session; the generation bump quarantines your stale messages automatically). Never just stop.
7. Do not modify registry, incoming, or lease files by hand; act only through the bundled CLI.
8. Do not pollute context by restating idle stdout messages at length.
9. On an unknown watcher event or exit code, fail closed: stop the current slice, report a `protocol_error`, and never retry an unknown event blindly.
10. Never expose provider, vendor, or model names in mailbox paths, artifact paths, or artifact contents — the `LWARn` alias is the only external identity.

Heartbeats are emitted by the watcher automatically; the agent never writes them.

## 2. Action Routing

Before performing an action for the first time this session, read its reference document in full. Do not act from this table alone.

| Action | Read first |
|---|---|
| `register [number]`, `response`, identity adoption | [references/register.md](references/register.md) |
| `adp` — the watch loop, stdout events, control commands | [references/adp-loop.md](references/adp-loop.md) |
| executing a claimed task, drafting and submitting results | [references/execute-complete.md](references/execute-complete.md) |
| `status`, `on`, `drain`, `off`, `unregister`, exhaustion handoff | [references/lifecycle.md](references/lifecycle.md) |

**Action name → actual CLI** (the hints are short labels; the CLI verbs differ):

| Hint / label | CLI command |
|---|---|
| `info` | `pao.py info` |
| `doctor` | `pao.py doctor --role lwar` |
| `register [number]` | `lwar.py register [number] --runtime-name … --model … --adapter-id … --vendor-family … --interface …` (register.md lists the required flags) |
| `response` | `lwar.py response REQUEST_ID` |
| `adp` | `adp_watch.py --identity-file <abs>` |
| `status` (this LWAR's own) | `lwar.py status --identity-file <abs>` (refreshes your identity state; use this, not `oa.py status`, for self-inspection) |
| `on` / `drain` / `off` | `lwar.py state on` / `lwar.py state draining` / `lwar.py state off` |
| `unregister` | `lwar.py state deregistered` (only from `off`, after OA reconcile) |

JSON Schemas for every bus message live in [schemas/](schemas/).
