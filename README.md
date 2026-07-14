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

## Installation and Deployment Modes

PAO runs in two modes:

- **Development mode** — work inside this repository; commands run as `python -m pao_runtime.*` or `python scripts/*.py` with an explicit `--root`.
- **Operation mode** — orchestrate from any project workspace against one central bus. No pip install required:

### 1. Copy the skills to your global skills directory

Copy `.agents/skills/oa-runtime` and `.agents/skills/lwar-runtime` into whichever global skills path your runtime loads — `~/.agents/skills` is the emerging cross-runtime convention, `~/.claude/skills` for Claude Code, or any location you prefer. A plain copy is all there is to it; `pao install-skills` does exactly the same copy if you prefer a command.

### 2. Set two environment variables

```bash
setx PAO_HOME <path-to-this-repository>   # where the runtime code lives
setx PAO_ROOT <central-bus-dir>           # bus root used when --root is omitted
```

### 3. Run from any workspace

The `scripts/*.py` wrappers bootstrap their own import path, so no installation is needed:

```bash
python "$PAO_HOME/scripts/lwar.py" register --runtime-name "Runtime" ...
python "$PAO_HOME/scripts/oa.py" status
python "$PAO_HOME/scripts/adp_watch.py" --identity-file <identity_file>
```

### Optional: pip console scripts

If you prefer short commands, `pip install -e <PAO_HOME>` provides `pao`, `pao-oa`, `pao-lwar`, and `pao-adp-watch`, and makes `python -m pao_runtime.*` importable from any directory. This is a convenience, not a requirement. `pao info` diagnoses version and root resolution either way.

Root resolution precedence: explicit `--root` > `PAO_ROOT` environment variable > current directory. The bus (`mailbox/`, `var/`, `control/`) stays central while each task executes in its own `cwd` — any project workspace can host an OA or LWAR session.

## Quick Start

### 1. Register an LWAR

```bash
python scripts/lwar.py register \
  --runtime-name "Runtime" \
  --model "Model" \
  --adapter-id runtime \
  --vendor-family vendor \
  --interface tui \
  --root .
```

To request a specific slot, use `register 1`. If omitted, OA assigns the lowest available number.

### 2. OA approval

```bash
python scripts/oa.py reconcile --root .
python scripts/lwar.py response <request_id> --root .
```

### 3. Run an ADP watch slice

```bash
python scripts/adp_watch.py \
  --identity-file <identity_file> \
  --root . \
  --interval 5 \
  --timeout 90
```

If the watcher reports `idle_timeout` or `state_wait`, the same LWAR session should immediately invoke it again. If it reports `task_received`, execute the task and submit the result with `lwar.py complete`.

## Documentation

- [Technical specification](docs/PAO_TechSpec.md)
- [ADP operations guide](docs/PAO_ADP_Operations.md)
- [Runtime bootstrap prompts](docs/LWAR_ADP_Bootstrap.md)
- [Canonical architecture](.pgf/DESIGN-PAO.md)
- [ADP design](.pgf/DESIGN-PAOADP.md)
- [Verification review](.pgf/REVIEW-PAOADP.md)

## Verification

```bash
python -m unittest discover -s tests -v
python -m py_compile pao_runtime/*.py scripts/*.py tests/*.py
```

The current integration suite (30 tests) verifies registration, collision rejection, full task/result flow, idle timeout behavior, off-state rejection, stale lease recovery, shutdown control, generation increments, retry budget and dead-letter transitions, stale/duplicate result quarantine, lease alignment, ledger lifecycle, heartbeat staleness, validation reporting, capability/load routing, cancel and priority flows, tombstone windows, pruning, audit logging, and `depends_on` gating.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
