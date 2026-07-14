# Agent runtime bootstrap

# Environment
- Bash: invoke `bash` from `PATH`.
- PowerShell 7: invoke `pwsh` from `PATH` (UTF-8). Old PowerShell 5.1 forbidden.
- Respond in Korean; keep code / commands / identifiers in English. Address the user as `Jeonguk-nim`.
- **Self-contained (development mode)**: when working on the PAO codebase in this repository, use ONLY this workspace's .agents/skills — never global skills.
- Python: run `python` (on PATH); calling the interpreter by absolute path is forbidden.

# Deployment modes

- **Development** (this repository): local skills, explicit `--root`, `python -m pao_runtime.*`.
- **Operation** (any project workspace): copy the two skills into your global skills directory (`~/.agents/skills` by convention, or wherever your runtime loads them), set `PAO_HOME` (code) and `PAO_ROOT` (central bus), then invoke `python "$PAO_HOME/scripts/*.py"` — the wrappers bootstrap their own import path, so no pip install is needed. CLIs resolve the bus as `--root` > `PAO_ROOT` > cwd; tasks execute in their own `cwd`. `pip install -e` and `pao install-skills` remain optional conveniences.

# Execution hygiene
- Avoid complex nested quoting in PowerShell one-liners. Prefer Git Bash for `rg`, `diff`, and shell pipelines, or use a short `python -c` command with simple quoting.
- When using PowerShell 7, always invoke `pwsh -NoProfile`; never rely on the default `powershell` shell.
- For Python import/API checks, avoid escaped print strings. Use minimal commands such as `python -c 'from pkg import Name; print(1)'`.
- For line-number extraction, prefer `rg -n` from Git Bash. If PowerShell is necessary, keep variables and `-f` formatting out of nested command strings.
- Treat shell quoting failures as command-construction errors first, not environment failures. Retry with a simpler command before reporting a dependency problem.
