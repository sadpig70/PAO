# PAO Plugin Package

This directory is the installable unit of PAO — Persistent Agent Orchestration: the `pao` Claude Code plugin and, equivalently, the `PAO_HOME` directory for manual installs.

- `skills/` — the OA and LWAR runtime contracts (with schemas and references)
- `commands/` — `/pao:oa` and `/pao:lwar-*` command aliases
- `scripts/` — self-bootstrapping CLI wrappers (`pao.py`, `oa.py`, `lwar.py`, `adp_watch.py`)
- `pao_runtime/` — the stdlib-only Python runtime (Python >= 3.10)
- `docs/` — technical specification and operation guides
- `.claude-plugin/plugin.json` — the plugin manifest

See the [repository README](../README.md) for architecture, installation, and verification.
