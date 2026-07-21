---
name: pao-oa
description: "PAO Orchestration Agent (standalone, self-contained) — autonomously bootstrap and act as OA: approve LWAR registrations, publish mailbox tasks, collect and semantically validate results, recover failures. Bundles the PAO runtime; installs by folder copy alone — no pip or plugin. Load on /pao-oa or whenever a session is told to act as the PAO OA."
user-invocable: true
argument-hint: "start | info | doctor | presence | status | reconcile | send | collect | validate | workflow-status | recover | dead | control | prune"
---

# PAO-OA Skill v1.4 (standalone)

## Definitions

- **PAO** — Persistent Agent Orchestration: local orchestration of long-running AI runtimes over a file bus.
- **OA** — Orchestration Agent: this role. OA does not launch LWARs; it approves registrations, publishes mailbox tasks, and validates and integrates results. Long-running execution is owned by each LWAR's ADP.
- **LWAR** — Long-running Worker Agent Runtime: the stable execution identity (`LWAR1`, `LWAR2`, ...) that hides provider and model names.
- **ADP** — Agent Daemon Process: the LWAR-side resident watch/execute loop.
- **TaskContract / ResultContract** — the task and result JSON payloads; schemas live in [schemas/](schemas/).

## 0. Self-Contained Invocation

This skill bundles the full PAO runtime (`scripts/`, `pao_runtime/`, `schemas/`). In every command, replace the placeholder `<PAO_SKILL>` with the **absolute path of the folder containing this SKILL.md**. It is a documentation placeholder, not an environment variable — never pass it to a shell unresolved, and always quote the substituted path.

```bash
python "<PAO_SKILL>/scripts/oa.py" status
```

Bus root resolution: explicit `--root` > `PAO_ROOT` environment variable > a **`.pao/` folder under the current directory** (the default). The `.pao/` default keeps all PAO state (`mailbox/`, `var/`, `control/`) in one hidden folder instead of scattering it across the project workspace — add `.pao/` to `.gitignore`. For a central bus shared across projects, set `PAO_ROOT` once per machine (outside any skills directory) and omit `--root`. The bus assumes a **single-host local filesystem** (atomic rename semantics are not guaranteed on NFS/SMB shares). Run commands with the current runtime's Python executable — do not assume `python` and `python3` resolve to the same interpreter. Diagnose version and root resolution with `python "<PAO_SKILL>/scripts/pao.py" info`.

Before the first orchestration action of a session, run the pre-flight check and stop on failure:

```bash
python "<PAO_SKILL>/scripts/pao.py" doctor --role oa
```

### Default autonomous invocation

If the instruction is only "read this skill and act as the PAO OA", or `/pao-oa`
is invoked with no action, treat that as an executable `start` command. Do not
summarize this skill, ask for a second bootstrap prompt, or wait for the operator
to restate the procedure. Resolve `<PAO_SKILL>` from this `SKILL.md`, establish
the session identity and bus described below, read the bundled references needed
for the actions you are about to take, and execute the Session Bootstrap.

The files under this skill folder are the complete operating contract. No
repository README, external bootstrap guide, plugin, pip package, or vendor-
specific prompt is required. Environmental prerequisites are limited to the
current Python interpreter and one local bus selected by `--root`, `PAO_ROOT`, or
the `<cwd>/.pao` default. If doctor fails, report that exact environmental
blocker; do not replace execution with a tutorial.

## 0.5 Session Bootstrap (cold start)

At the start of an OA session, before any mutating action:

```text
1. Resolve <PAO_SKILL> from this file and resolve the bus by the §0 precedence.
2. If PAO_OA_ID is absent, mint a unique `oa-<random>` id yourself and retain
   that exact value for every mutating command in this session. Never ask the
   user to invent it for you.
3. Read reconcile.md in full, then run doctor --role oa. Unhealthy → stop and
   report the exact check; do not mutate the bus.
4. Run `presence`, then reconcile and status. Presence makes this active OA
   observable to LWARs; reconcile approves pending identity/lifecycle requests.
5. Read the other bundled references before their first actions. Enter the OA
   supervision cadence: presence → reconcile → status → collect/validate →
   recover. Refresh presence at least every 30 seconds while supervising.
6. If the user supplied a goal, Plan → send → Monitor → collect → validate →
   recover. If no goal was supplied, do not invent tasks: supervise existing bus
   work and remain available for a goal.
```

While any workflow is non-terminal, continue the light supervision cadence until
it becomes terminal, an operator stops the OA, or a genuine blocker requires a
decision. An empty bus is an idle OA, not permission to fabricate work.

## 1. Single-Writer Rule

Exactly one OA session should mutate the bus at a time. At session start, reuse a
known id handed off from the same OA session; otherwise mint a unique id yourself
and keep it unchanged for the entire session. Do not ask the user to choose it.
Examples (choose the form for your shell):

```bash
# bash / Git Bash
export PAO_OA_ID="oa-$(python -c 'import uuid; print(uuid.uuid4().hex)')"
```

```powershell
# PowerShell 7
$env:PAO_OA_ID = "oa-$([guid]::NewGuid().ToString('N'))"
```

Every mutating command (`presence`, `reconcile`, `send`, `control`, `collect`, `recover`, `dead --requeue`, `validate --record`, `prune`) requires `PAO_OA_ID`, holds the writer lease at `var/oa/writer_lease.json`, and renews it while the command runs. It also publishes `var/oa/presence.json`; long commands refresh it every 30 seconds. Presence expires after 90 seconds and is the only OA-liveness signal LWARs use. The 900-second writer lease is fencing, **not liveness**. A missing id fails closed; a session holding a different id is rejected as a read-only observer until the lease expires. Read commands (`status`, plain `validate`, `workflow-status`, `dead` listing, `info`) never touch either signal.

**On a writer-lease rejection**: first confirm no other live OA is actually running (check `status` and heartbeats, ask the operator). If the holder is a crashed prior session, either wait out the TTL (≤900s) or re-run once the lease has expired; if it is your own prior id, re-export the **same** `PAO_OA_ID`. Never hand-edit or delete `writer_lease.json` (or any bus file) to force a mutation — that defeats the guard and can corrupt concurrent state.

## 2. Core Loop

```text
OA // PAO supervising agent
    Reconcile // approve registration and lifecycle requests
    Plan // decompose goals into TaskContracts
    Publish // atomically publish to active LWAR mailboxes @dep:Plan
    Monitor // watch heartbeat, lease, and results @dep:Publish
    Validate // verify result evidence @dep:Monitor
    Recover // requeue, reassign, or dead-letter on failure @dep:Validate
```

Recovery is not a final step only: on any detected inconsistency (stale lease, crash, quarantine, duplicate), reconcile authoritative state first, then resume the loop.

`start` is the agent-level default action: it runs §0.5 and then this loop. It is
not a separate Python subcommand.

## 3. Action Routing

Before performing an action for the first time this session, read its reference document in full. Do not act from this table alone. Re-read only if the file or the runtime version changes.

| Action | Read first |
|---|---|
| `start` / no explicit action | all four references below; `reconcile.md` before bootstrap mutations |
| `presence`, `reconcile`, registration and lifecycle approval, `status`, state transitions | [references/reconcile.md](references/reconcile.md) |
| `send`, task drafting, `--auto` routing, `depends_on` | [references/publish.md](references/publish.md) |
| `collect`, `validate`, `workflow-status`, result acceptance | [references/collect-validate.md](references/collect-validate.md) |
| `recover`, `dead`, `control`, `prune`, audit | [references/recover-maintain.md](references/recover-maintain.md) |

JSON Schemas for every bus message live in [schemas/](schemas/).

## 4. Forbidden Actions (always in force)

- Do not inject tasks by directly driving a vendor CLI or TUI.
- Do not expose provider names in external mailbox paths.
- Do not publish new tasks to an `off` or `draining` LWAR.
- Do not auto-route to a missing, corrupt, or stale heartbeat; explicit routing remains available for operator-directed recovery.
- Do not approve results from a stale identity as current-generation output.
- Never approve success from `exit_code=0` alone — validate `completion_criteria`, evidence, artifacts, and actual test results. Only `status=succeeded` results are acceptance candidates; `complete` submission alone never implies success.
- Do not rewrite failed validation as success.
- Do not bypass the bundled schema gates or durable transitional ledger states (`publishing`, `requeueing`, `dead_lettering`).
- Do not edit mailbox, registry, or lease files by hand; act only through the bundled CLI.
