![PAO — Persistent Agent Orchestration](assets/PAO_hero.png)

# PAO

**Persistent Agent Orchestration** is a local orchestration system that coordinates heterogeneous long-running AI runtimes behind a single external identity model, `LWARn`, over a file-based message bus.

PAO does not force vendor CLIs into non-interactive execution. Each runtime session is started by the user, then repeatedly calls an **ADP (Agent Daemon Process)** watcher to receive work and return results inside the same conversational context.

## Architecture

```text
OA (Orchestration Agent)
  └─ Task JSON → mailbox/LWARn/incoming/
                         ↓ atomic claim
              LWAR long-running session
                         ↓ ADP watch/execute loop
  └─ Result JSON ← mailbox/LWARn/outgoing/
```

- **OA**: approves registrations, publishes tasks and controls, collects results, and recovers expired leases
- **LWAR**: stable execution identity that hides provider and model names (`LWAR1`, `LWAR2`, ...)
- **ADP**: resident mailbox loop built from 5-second polling and 90-second watch slices
- **File bus**: atomic JSON publish/claim flow with heartbeat, generation, and lease semantics

## Key Properties

- `/lwar-register [number]` self-registration with optional automatic lowest-number allocation
- stale-message isolation by `lwar_id + instance_id + generation`
- atomic bounded LWAR startup: `response --resident` adopts identity and enters
  the watcher in one Python process; OA distinguishes registered-not-started
  from active-then-stale, and auto-routing waits for the first operational
  heartbeat
- lifecycle transitions: `on → draining → off → deregistered`
- support for long-running runtimes, including TUIs
- provider-neutral task and result contracts
- lease recovery plus generation bumps when aliases are reused
- retry budget enforcement with a dead-letter queue (`dead/`, `oa_cli dead --requeue`)
- stale and duplicate result quarantine at collection time
- claim leases aligned with each task's `timeout_s`
- durable OA task ledger (`var/tasks/`) with `validate` and `workflow-status` commands
- capability- and load-based automatic routing (`send --auto --require-capability`)
- `depends_on` task gating for simple workflow DAGs
- append-only audit log (`var/audit/events.jsonl`) and archive pruning (`prune`)
- replaceable message plane: the `Transport` protocol with `FileTransport` as the local implementation
- distributed as two self-contained skills (`.agents/skills/pao-oa`, `.agents/skills/pao-lwar`): each bundles the contract, wrapper scripts, and the full stdlib-only runtime; installs by folder copy alone (no pip, no plugin). Vendor-neutral — proven on Claude Code and Kimi Code CLI

## Installation and Deployment Modes

PAO is distributed as two **self-contained skills** — `.agents/skills/pao-oa` and
`.agents/skills/pao-lwar`. Each bundles the OA/LWAR contract, the wrapper scripts,
and the full stdlib-only runtime (plus message schemas for the LWAR). There is
**one channel**; installation is a folder copy — no pip, no plugin.

### Install (folder copy)

Copy the two skill folders into whichever global skills path your runtime loads
— `~/.claude/skills` for Claude Code, `~/.agents/skills` (the emerging
cross-runtime convention), or any location you prefer:

```bash
cp -r .agents/skills/pao-oa .agents/skills/pao-lwar ~/.claude/skills/
```

Invocation is namespace-free: `/pao-oa`, `/pao-lwar`. In a shell, call the
wrapper scripts by their absolute path (they bootstrap their own import path, so
no install is needed): `python "<skill>/scripts/oa.py" …`,
`python "<skill>/scripts/lwar.py" …`.

### Bus root

Root resolution precedence: explicit `--root` > `PAO_ROOT` environment variable
> a **`.pao/` folder under the current directory** (the default). The `.pao/`
default keeps all PAO state (`mailbox/`, `var/`, `control/`) in one hidden,
gitignorable folder instead of scattering it across the project workspace. Set
`PAO_ROOT` to a central bus shared across projects instead. Each task executes in
its own `cwd` — any project workspace can host an OA or LWAR session.

### Canonical source

`.agents/skills/pao-lwar` is the **runtime master**; edit `pao_runtime/`, `scripts/`,
or `schemas/` only there, then run `python tools/sync_bundles.py` to mirror
into `pao-oa`. The test suite byte-verifies the two bundles match. `pao info`
diagnoses version and root resolution; `pao doctor --role oa|lwar` is a
pre-flight check.

## Quick Start

Start the two agent runtimes in the same project directory, or give both the same
`PAO_ROOT`. Each runtime needs only its role skill; the skill owns bootstrap and
all later commands.

### 1. Start OA

```text
Read <absolute-path>/pao-oa/SKILL.md and act as the PAO OA.
```

### 2. Start LWAR

```text
Read <absolute-path>/pao-lwar/SKILL.md and act as a PAO LWAR.
```

No registration prompt, ADP prompt, or copied command sequence is required. The
two `SKILL.md` files and their bundled references are the sole operating contract.

## Documentation

- [Technical specification](docs/PAO_TechSpec.md)
- [ADP operations guide](docs/PAO_ADP_Operations.md)
- [Skill-only bootstrap note](docs/LWAR_ADP_Bootstrap.md)

## Verification

```bash
python -m unittest discover -s tests -v
python -m py_compile .agents/skills/pao-lwar/pao_runtime/*.py .agents/skills/pao-lwar/scripts/*.py tests/*.py
```

The integration suite verifies registration, collision rejection, bounded startup classification, current-generation heartbeat fencing, full task/result flow, resident idle heartbeat continuity, compatibility idle-timeout behavior, off-state rejection, stale lease recovery, shutdown and clean-retire control, OA presence classification, generation increments, retry budget and dead-letter transitions, stale/duplicate result quarantine, lease alignment, ledger lifecycle, heartbeat staleness, validation reporting, capability/load routing, cancel and priority flows, tombstone windows, pruning, audit logging, `depends_on` gating, attempt fencing, artifact provenance, authority bounds, single-writer OA lease, the `.pao/` default root and portability, the graded-correctness axis, and the two-bundle byte sync.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
