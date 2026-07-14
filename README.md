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
- **ADP**: resident mailbox loop built from 1-second polling and 90-second watch slices
- **File bus**: atomic JSON publish/claim flow with heartbeat, generation, and lease semantics

## Key Properties

- `/lwar-register [number]` self-registration with optional automatic lowest-number allocation
- stale-message isolation by `lwar_id + instance_id + generation`
- lifecycle transitions: `on → draining → off → deregistered`
- support for long-running runtimes, including TUIs
- provider-neutral task and result contracts
- lease recovery plus generation bumps when aliases are reused

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
  --interval 1 \
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

The current integration suite verifies registration, collision rejection, full task/result flow, idle timeout behavior, off-state rejection, stale lease recovery, shutdown control, and generation increments.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
