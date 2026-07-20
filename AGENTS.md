# Agent runtime bootstrap

# Environment
- Bash: invoke `bash` from `PATH`.
- PowerShell 7: invoke `pwsh` from `PATH` (UTF-8). Old PowerShell 5.1 forbidden.
- Respond in Korean; keep code / commands / identifiers in English. Address the user as `Jeonguk-nim`.
- **Self-contained (development mode)**: when working on the PAO codebase in this repository, use ONLY this workspace's skills under `.agents/skills/` (`pao-oa`/`pao-lwar` are the PAO product; `pg`/`pgf`/`pgxf` are dev tooling) — never global skills.
- Python: run `python` (on PATH); calling the interpreter by absolute path is forbidden.

# Deployment modes

- **Single canonical channel (since 2026-07-20)**: `.agents/skills/pao-oa` + `.agents/skills/pao-lwar` are the only distribution channel; `.agents/skills/pao-lwar` is the runtime master. `.agents/skills/pao-oa`'s runtime layer is a **generated mirror** — never edit it directly; edit the master (`pao_runtime/`, `scripts/`, `schemas/`), then run `python tools/sync_bundles.py`. Authored per-skill files (SKILL.md, references) are edited in place. The two-bundle byte-sync test fails on any drift. (The former Claude Code plugin was retired to `_legacy/PAO_plugin/`, untracked; git history retains it.)
- **Development** (this repository): local skills, explicit `--root`, `python .agents/skills/pao-oa/scripts/*.py` / `python .agents/skills/pao-lwar/scripts/*.py`.
- **Operation** (any project workspace): copy `.agents/skills/pao-oa` and `.agents/skills/pao-lwar` into a global skills directory (`~/.claude/skills`, `~/.agents/skills`, or any path your runtime loads); each bundles the full runtime, so nothing but the bus root is needed. Invoked as `/pao-oa`, `/pao-lwar`. Bus root resolves as `--root` > `PAO_ROOT` > `<cwd>/.pao` (the default); tasks execute in their own `cwd`. The wrappers bootstrap their own import path — no pip install. Vendor-neutral: proven on Claude Code and Kimi Code CLI.

# Execution hygiene
- Avoid complex nested quoting in PowerShell one-liners. Prefer Git Bash for `rg`, `diff`, and shell pipelines, or use a short `python -c` command with simple quoting.
- When using PowerShell 7, always invoke `pwsh -NoProfile`; never rely on the default `powershell` shell.
- For Python import/API checks, avoid escaped print strings. Use minimal commands such as `python -c 'from pkg import Name; print(1)'`.
- For line-number extraction, prefer `rg -n` from Git Bash. If PowerShell is necessary, keep variables and `-f` formatting out of nested command strings.
- Treat shell quoting failures as command-construction errors first, not environment failures. Retry with a simpler command before reporting a dependency problem.
