---
name: pao-lwar
description: "PAO LWAR (standalone, self-contained) — autonomously bootstrap, self-register, adopt an approved identity, and remain in the ADP watch/execute loop. Bundles the PAO runtime; installs by folder copy alone — no pip or plugin. Load on /pao-lwar or whenever a session is told to act as a PAO LWAR."
user-invocable: true
argument-hint: "start | info | doctor | oa-status | register [number] | response | adp | status | on | drain | off | retire | unregister"
---

# PAO-LWAR Skill v1.5 (standalone)

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

Bus root resolution before identity adoption (`doctor`, `register`, `response`) is explicit `--root` > `PAO_ROOT` > a **`.pao/` folder under the current directory**. Adoption persists the canonical `bus_root` in the identity. Afterwards, `status`, `state`, `complete`, and ADP derive the bus from `--identity-file` when neither `--root` nor `PAO_ROOT` is supplied. An explicit/env root may repeat the identity root but a mismatch fails closed before touching the conflicting bus. Legacy identities under `<root>/var/identities/` derive the root from that canonical location. Task execution still happens in each task's own `cwd`; only the bus location is identity-bound. The bus requires a **single-host local filesystem**. Run commands with the current runtime's Python executable — do not assume `python` and `python3` resolve to the same interpreter.

Before registering or starting ADP, run the pre-flight check and stop on failure:

```bash
python "<PAO_SKILL>/scripts/pao.py" doctor --role lwar
```

### Default autonomous invocation

If the instruction is only "read this skill and act as a PAO LWAR", or
`/pao-lwar` is invoked with no action, treat that as an executable `start`
command. Do not summarize this skill, ask for a second bootstrap prompt, or wait
for the operator to restate the procedure. Resolve `<PAO_SKILL>` from this
`SKILL.md`, read all four bundled reference documents in full, execute the
Session Bootstrap, and remain inside ADP until a documented terminator occurs.

The files under this skill folder are the complete operating contract. No
repository README, external bootstrap guide, plugin, pip package, or vendor-
specific prompt is required. Environmental prerequisites are limited to the
current Python interpreter, one local bus selected by `--root`, `PAO_ROOT`, or
the `<cwd>/.pao` default. OA and LWAR may start in either order: an absent OA is
an observable wait state, not a bootstrap failure. If doctor fails, report the
exact event; do not replace execution with a tutorial.

## 0.5 Session Bootstrap (cold start)

Run this decision flow at the start of a session, before any other action:

```text
1. Resolve <PAO_SKILL> from this file and resolve the pre-adoption bus by §0.
2. Build a truthful runtime profile from information already available to this
   session. Use register.md's explicit `unreported` sentinels for unavailable
   fields; do not ask for a second bootstrap prompt and do not invent capabilities.
3. doctor --role lwar   → unhealthy? stop and report.
4. Run `lwar.py oa-status`. Record `live`, `stale`, `missing`, or `invalid`.
   Only `live` proves an OA is currently supervising. Any other state does not
   block registration: continue, then wait for OA reconciliation without
   self-assigning a slot. OA may start later.
5. Did this session receive one explicit absolute identity_file through a
   trusted handoff or its own earlier `identity_adopted` response?
   If so, run `lwar.py status --identity-file <that file>` and branch on its EXIT
   CODE (do not collapse them to present/absent — see lifecycle.md):
     exit 0 (slot present, tuple matches) → RESUME: skip registration. If state is
            not `on`, request `state on` (lifecycle.md), poll until on; then ADP.
     exit 2 (registry unavailable) → TRANSIENT: wait briefly and retry status; do
            NOT register (that would orphan a still-valid identity).
     exit 3 (unregistered) → REGISTER (see below).
     exit 4 (identity mismatch / slot reused) → your identity is stale: REGISTER
            fresh; do not reuse the stale identity file.
   No explicit identity handle → REGISTER fresh. Never scan `var/identities/`,
   guess ownership from filenames, or adopt another session's identity. Run
   `lwar.py register …` (register.md),
     remember request_id, poll `lwar.py response REQUEST_ID` until `identity_adopted`,
     keep the printed identity_file (absolute) for every later call.
6. Enter the ADP loop (adp-loop.md) and keep it alive until a documented
   terminator. Never return a completion summary while ADP should remain resident.
```

Never re-register when a valid identity already exists — a needless re-registration
takes a new slot/generation and orphans the old one. All `--identity-file` and
`--result-file` arguments must be **absolute paths** (they resolve against the
process cwd, not the bus root).

`start` is the agent-level default action: it runs this bootstrap and ADP. It is
not a separate Python subcommand.

## 1. Absolute Rules

1. Before registering, read [references/register.md](references/register.md). Before the **first** watch slice, read [references/adp-loop.md](references/adp-loop.md) **in full** — the exit-code contract, lease alignment, and stale-identity rejection are pre-loop knowledge, not lookup-on-event material. Read each reference in full once per session before its first use; re-read only if the file or the runtime version changes.
2. Use only the approved `(lwar_id, instance_id, generation)` as your runtime identity. Never claim an `LWARn` identity before approval.
3. **ADP is a Daemon — stay resident.** The **D** in ADP is **Daemon**: like an OS daemon, this loop runs continuously and **never exits on its own**. Do not assume an external process will relaunch the LWAR — keep ADP alive inside the current session. Elapsed time, many repeated `idle_timeout`/`state_wait` slices, a sense that "enough has happened", or the instinct to summarize and wrap up are **NOT** reasons to stop — a daemon that quits because it feels finished is a bug. Actively resist the natural tendency to conclude: immediately re-invoke the next watch slice, and **never return control or end your turn between slices**. The daemon terminates **only** on (a) `control:shutdown`, (b) successful `control:retire`, (c) a fatal `adp_error`, or (d) the context-exhaustion handoff ([references/lifecycle.md](references/lifecycle.md)). If you find yourself about to write a closing summary while none of these has occurred, do not — run the next slice instead.
4. On `idle_timeout` and `state_wait`, generate no extra explanation. Re-run the same watcher immediately.
5. On `task_received`, operate only within the TaskContract authority bounds and submit **exactly one terminal result** with `complete` before returning to the watcher, whenever this agent remains capable of submitting one. `complete` means terminal submission, **not success** — `failed`, `blocked`, `cancelled`, `timed_out`, and `protocol_error` outcomes are all submitted the same way. A crash or forced termination is recovered by lease expiry and OA `recover`; it is never inferred as success.
6. ADP terminates on exactly the four conditions in Rule 3 and nothing else. For `retire`, stop only after `lwar.py retire` reports `lwar_retired`; `retire_waiting` means OA reconciliation is still required. For the handoff: only on an **objective** exhaustion signal (an explicit runtime context/token warning, not elapsed time or a feeling of being done), execute the procedure in [references/lifecycle.md](references/lifecycle.md). Never just stop.
7. Do not modify registry, incoming, or lease files by hand; act only through the bundled CLI.
8. Do not pollute context by restating idle stdout messages at length.
9. On an unknown watcher event or exit code, fail closed **on the slice, not the daemon**: end only the current slice; if a task is claimed, submit a `protocol_error` terminal result for it; then run the next watch slice. Never retry the unknown event blindly, and never treat it as a reason to terminate ADP (only Rule 3's four conditions do that).
10. Never expose provider, vendor, or model names in result metadata, mailbox paths, artifact paths, or artifact contents — the `LWARn` alias is the only external identity. `complete` enforces this against the registered runtime profile.
11. Preserve the exact `task.claim_token` emitted by `task_received` and pass it to `complete`. A recovered/requeued claim has a new token; an old worker must fail closed instead of submitting into the new attempt.
12. Treat the adopted identity's `bus_root` as immutable authority. Never redirect that identity to another bus; a root conflict is a fatal configuration error.

Heartbeats are emitted by the watcher automatically; the agent never writes them.

## 2. Action Routing

Before performing an action for the first time this session, read its reference document in full. Do not act from this table alone.

| Action | Read first |
|---|---|
| `start` / no explicit action | all four references below, then §0.5 |
| `register [number]`, `response`, identity adoption | [references/register.md](references/register.md) |
| `adp` — the watch loop, stdout events, control commands | [references/adp-loop.md](references/adp-loop.md) |
| executing a claimed task, drafting and submitting results | [references/execute-complete.md](references/execute-complete.md) |
| `oa-status`, `status`, `on`, `drain`, `off`, `retire`, `unregister`, exhaustion handoff | [references/lifecycle.md](references/lifecycle.md) |

**Action name → actual CLI** (the hints are short labels; the CLI verbs differ):

| Hint / label | CLI command |
|---|---|
| `info` | `pao.py info` |
| `doctor` | `pao.py doctor --role lwar` |
| `oa-status` | `lwar.py oa-status` before adoption; add `--identity-file <abs>` after adoption |
| `register [number]` | `lwar.py register [number] --runtime-name … --model … --adapter-id … --vendor-family … --interface …` (register.md lists the required flags) |
| `response` | `lwar.py response REQUEST_ID` |
| `adp` | `adp_watch.py --identity-file <abs>` |
| `status` (this LWAR's own) | `lwar.py status --identity-file <abs>` (refreshes your identity state; use this, not `oa.py status`, for self-inspection) |
| `on` / `drain` / `off` | `lwar.py state on` / `lwar.py state draining` / `lwar.py state off` |
| `retire` | repeatedly run `lwar.py retire --identity-file <abs>` until `lwar_retired`; OA reconciles each requested transition |
| `unregister` | `lwar.py state deregistered` (only from `off`, after OA reconcile) |

JSON Schemas for every bus message live in [schemas/](schemas/).
The runtime validates them at every registration, lifecycle, mailbox, heartbeat,
lease, task, control, result, and identity trust boundary.
