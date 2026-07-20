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

Bus root resolution: explicit `--root` > `PAO_ROOT` environment variable > a **`.pao/` folder under the current directory** (the default). The `.pao/` default keeps all PAO state (`mailbox/`, `var/`, `control/`) in one hidden folder instead of scattering it across the workspace — add `.pao/` to `.gitignore`. In operation mode set `PAO_ROOT` to a central bus and omit `--root`; task execution still happens in each task's own `cwd` (which is unaffected — only the bus moves under `.pao/`). The bus assumes a **single-host local filesystem** (atomic rename semantics are not guaranteed on NFS/SMB shares). Run commands with the current runtime's Python executable — do not assume `python` and `python3` resolve to the same interpreter.

Before registering or starting ADP, run the pre-flight check and stop on failure:

```bash
python "<PAO_SKILL>/scripts/pao.py" doctor --role lwar
```

## 0.5 Session Bootstrap (cold start)

Run this decision flow at the start of a session, before any other action:

```text
1. doctor --role lwar   → unhealthy? stop and report.
2. Do you already hold an identity file from a prior session (var/identities/<instance>.json)?
   If so, run `lwar.py status --identity-file <that file>` and branch on its EXIT
   CODE (do not collapse them to present/absent — see lifecycle.md):
     exit 0 (slot present, tuple matches) → RESUME: skip registration. If state is
            not `on`, request `state on` (lifecycle.md), poll until on; then ADP.
     exit 2 (registry unavailable) → TRANSIENT: wait briefly and retry status; do
            NOT register (that would orphan a still-valid identity).
     exit 3 (unregistered) → REGISTER (see below).
     exit 4 (identity mismatch / slot reused) → your identity is stale: REGISTER
            fresh; do not reuse the stale identity file.
   No identity file at all → REGISTER: run `lwar.py register …` (register.md),
     remember request_id, poll `lwar.py response REQUEST_ID` until `identity_adopted`,
     keep the printed identity_file (absolute) for every later call.
3. Enter the ADP loop (adp-loop.md) and keep it alive until `shutdown`.
```

Never re-register when a valid identity already exists — a needless re-registration
takes a new slot/generation and orphans the old one. All `--identity-file` and
`--result-file` arguments must be **absolute paths** (they resolve against the
process cwd, not the bus root).

## 1. Absolute Rules

1. Before registering, read [references/register.md](references/register.md). Before the **first** watch slice, read [references/adp-loop.md](references/adp-loop.md) **in full** — the exit-code contract, lease alignment, and stale-identity rejection are pre-loop knowledge, not lookup-on-event material. Read each reference in full once per session before its first use; re-read only if the file or the runtime version changes.
2. Use only the approved `(lwar_id, instance_id, generation)` as your runtime identity. Never claim an `LWARn` identity before approval.
3. **ADP is a Daemon — stay resident.** The **D** in ADP is **Daemon**: like an OS daemon, this loop runs continuously and **never exits on its own**. Do not assume an external process will relaunch the LWAR — keep ADP alive inside the current session. Elapsed time, many repeated `idle_timeout`/`state_wait` slices, a sense that "enough has happened", or the instinct to summarize and wrap up are **NOT** reasons to stop — a daemon that quits because it feels finished is a bug. Actively resist the natural tendency to conclude: immediately re-invoke the next watch slice, and **never return control or end your turn between slices**. The daemon terminates **only** on (a) `control:shutdown`, (b) a fatal `adp_error`, or (c) the context-exhaustion handoff ([references/lifecycle.md](references/lifecycle.md)). If you find yourself about to write a closing summary while none of these has occurred, do not — run the next slice instead.
4. On `idle_timeout` and `state_wait`, generate no extra explanation. Re-run the same watcher immediately.
5. On `task_received`, operate only within the TaskContract authority bounds and submit **exactly one terminal result** with `complete` before returning to the watcher, whenever this agent remains capable of submitting one. `complete` means terminal submission, **not success** — `failed`, `blocked`, `cancelled`, `timed_out`, and `protocol_error` outcomes are all submitted the same way. A crash or forced termination is recovered by lease expiry and OA `recover`; it is never inferred as success.
6. ADP terminates on exactly the three conditions in Rule 3 — `control:shutdown`, a fatal `adp_error` (watcher exit 30), or the context-exhaustion handoff — and nothing else. For the handoff: only on an **objective** exhaustion signal (an explicit runtime context/token warning, not elapsed time or a feeling of being done), execute the procedure in [references/lifecycle.md](references/lifecycle.md) (request `state draining`, submit the terminal result for any claimed task, then request `state off` or re-register from a fresh session; the generation bump quarantines your stale messages automatically). Never just stop.
7. Do not modify registry, incoming, or lease files by hand; act only through the bundled CLI.
8. Do not pollute context by restating idle stdout messages at length.
9. On an unknown watcher event or exit code, fail closed **on the slice, not the daemon**: end only the current slice; if a task is claimed, submit a `protocol_error` terminal result for it; then run the next watch slice. Never retry the unknown event blindly, and never treat it as a reason to terminate ADP (only Rule 3's three conditions do that).
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
