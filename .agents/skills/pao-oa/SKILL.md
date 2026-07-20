---
name: pao-oa
description: "PAO Orchestration Agent (standalone, self-contained) — approve LWAR registrations, publish mailbox tasks, collect and validate results, recover failures. Bundles the PAO runtime; installs by folder copy alone — no pip, no plugin, no environment variable besides PAO_ROOT. Load on /pao-oa or whenever acting as the PAO OA."
user-invocable: true
argument-hint: "info | doctor | status | reconcile | send | collect | validate | workflow-status | recover | dead | control | prune"
---

# PAO-OA Skill v1.1 (standalone)

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

## 0.5 Session Bootstrap (cold start)

At the start of an OA session, before any mutating action:

```text
1. Set PAO_OA_ID (see §1) and PAO_ROOT (or plan to pass --root everywhere).
2. doctor --role oa   → unhealthy? stop and report.
3. reconcile          → approve any pending registration/lifecycle requests.
4. status             → see the current LWAR roster, states, heartbeat staleness.
5. Then act on the user's goal: Plan → send → Monitor → collect → validate → recover.
```

`/pao-oa` with no explicit action defaults to this bootstrap (through step 4), then
waits for the user's goal.

## 1. Single-Writer Rule

Exactly one OA session should mutate the bus at a time. At session start, set a unique id once (choose the form for your shell):

```bash
# bash / Git Bash
export PAO_OA_ID=oa-$(date +%s)
```

```powershell
# PowerShell 7
$env:PAO_OA_ID = "oa-" + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
```

Every mutating command (`reconcile`, `send`, `control`, `collect`, `recover`, `dead --requeue`, `validate --record`, `prune`) refreshes the writer lease at `var/oa/writer_lease.json`; a session holding a different `PAO_OA_ID` is rejected as a read-only observer until the lease expires (TTL 900s). Read commands (`status`, plain `validate`, `workflow-status`, `dead` listing, `info`) never touch the lease. Sessions that skip `PAO_OA_ID` share the `oa-default` holder and get **no** mutual exclusion — setting the id is what activates the guarantee.

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

## 3. Action Routing

Before performing an action for the first time this session, read its reference document in full. Do not act from this table alone. Re-read only if the file or the runtime version changes.

| Action | Read first |
|---|---|
| `reconcile`, registration and lifecycle approval, `status`, state transitions | [references/reconcile.md](references/reconcile.md) |
| `send`, task drafting, `--auto` routing, `depends_on` | [references/publish.md](references/publish.md) |
| `collect`, `validate`, `workflow-status`, result acceptance | [references/collect-validate.md](references/collect-validate.md) |
| `recover`, `dead`, `control`, `prune`, audit | [references/recover-maintain.md](references/recover-maintain.md) |

JSON Schemas for every bus message live in [schemas/](schemas/).

## 4. Forbidden Actions (always in force)

- Do not inject tasks by directly driving a vendor CLI or TUI.
- Do not expose provider names in external mailbox paths.
- Do not publish new tasks to an `off` or `draining` LWAR.
- Do not approve results from a stale identity as current-generation output.
- Never approve success from `exit_code=0` alone — validate `completion_criteria`, evidence, artifacts, and actual test results. Only `status=succeeded` results are acceptance candidates; `complete` submission alone never implies success.
- Do not rewrite failed validation as success.
- Do not edit mailbox, registry, or lease files by hand; act only through the bundled CLI.
