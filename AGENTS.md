# Agent runtime bootstrap

# Environment
- Bash: invoke `bash` from `PATH`.
- PowerShell 7: invoke `pwsh` from `PATH` (UTF-8). Old PowerShell 5.1 forbidden.
- Respond in Korean; keep code / commands / identifiers in English. Address the user as `Jeonguk-nim`.
- **Self-contained (development mode)**: when working on the PAO codebase in this repository, use ONLY this workspace's skills — `PAO_skills/` for the OA/LWAR contracts, `.agents/skills/` for dev tooling — never global skills.
- Python: run `python` (on PATH); calling the interpreter by absolute path is forbidden.

# Deployment modes

- **Source of truth (current)**: `PAO_skills/` is the canonical source. Modify, operate, and verify directly in `PAO_skills/pao-oa` and `PAO_skills/pao-lwar`. `PAO_plugin/` is **frozen** — do not edit it — until the skills channel is verified in operation, after which verified changes are back-ported to the plugin in one pass.
- **Development** (this repository): local skills, explicit `--root`, `python PAO_skills/pao-oa/scripts/*.py` / `python PAO_skills/pao-lwar/scripts/*.py`.
- **Operation** (any project workspace): three channels. (a) **Claude Code plugin** — install `PAO_plugin/`; skills, `/pao:*` command aliases, and the runtime ship as one unit, and the skills reveal the plugin root via `${CLAUDE_PLUGIN_ROOT}` so `PAO_HOME` is unnecessary. (b) **Standalone skills** — copy `PAO_skills/pao-oa` and `PAO_skills/pao-lwar` into a global skills directory; each bundles the runtime (the canonical source during the current skills-first phase), so nothing but `PAO_ROOT` is needed; invoked as `/pao-oa`, `/pao-lwar`. (c) **Thin contract copy** — copy the two skills from `PAO_plugin/skills/` and set `PAO_HOME` to the `PAO_plugin` directory. In every channel set `PAO_ROOT` (central bus, outside plugin and skills directories); the wrappers bootstrap their own import path, so no pip install is needed. CLIs resolve the bus as `--root` > `PAO_ROOT` > cwd; tasks execute in their own `cwd`. `pip install -e` and `pao install-skills` remain optional conveniences.

# Execution hygiene
- Avoid complex nested quoting in PowerShell one-liners. Prefer Git Bash for `rg`, `diff`, and shell pipelines, or use a short `python -c` command with simple quoting.
- When using PowerShell 7, always invoke `pwsh -NoProfile`; never rely on the default `powershell` shell.
- For Python import/API checks, avoid escaped print strings. Use minimal commands such as `python -c 'from pkg import Name; print(1)'`.
- For line-number extraction, prefer `rg -n` from Git Bash. If PowerShell is necessary, keep variables and `-f` formatting out of nested command strings.
- Treat shell quoting failures as command-construction errors first, not environment failures. Retry with a simpler command before reporting a dependency problem.
