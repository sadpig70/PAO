# DESIGN-PAOSkills

> Goal: a second, parallel PAO operation channel — a **skills-only system** at `PAO_skills/` with two self-contained skills, `pao-oa` and `pao-lwar`. Installing = copying a skill folder into any runtime's global skills directory. No plugin machinery, no `PAO_HOME`, no pip; the only remaining environment variable is `PAO_ROOT` (central bus).
> Approved decisions (2026-07-16): variant B self-contained (runtime bundled per skill) + hash sync gates + lowercase kebab names. The plugin system (`PAO_plugin/`) continues unchanged.

## Constraints and invariants

- **Self-containment**: each skill folder bundles `scripts/` + `pao_runtime/`; `pao-lwar` also bundles `schemas/`. The wrappers resolve imports via `parents[1]` — inside a skill folder that parent is the skill folder itself, so copies work anywhere with zero configuration.
- **Canonical source**: `PAO_plugin/pao_runtime`, `PAO_plugin/scripts`, and `PAO_plugin/skills/lwar-runtime/schemas` remain the only hand-edited copies. The bundled copies inside `PAO_skills/` are **generated** by a sync command and **gate-checked byte-for-byte** by tests. Hand-editing them is forbidden.
- **Plugin-agnostic**: nothing under `PAO_skills/` may reference `${CLAUDE_PLUGIN_ROOT}`, `PAO_HOME`, or `python -m pao_runtime`. Path convention: `$PAO_SKILL` = the directory containing the loaded SKILL.md; every command runs `python "$PAO_SKILL/scripts/<cli>.py"`.
- **Lean SKILL.md**: each SKILL.md holds identity, absolute rules, and an action→reference routing table (~60 lines); procedures live in `references/*.md`, read on demand before performing that action.
- Standalone skills are invoked without a namespace: `/pao-oa`, `/pao-lwar`.
- Bus semantics untouched: `--root` > `PAO_ROOT` > cwd; bus lives outside any skills directory.

## Gantree

```text
PAOSkills // standalone skills-only PAO channel (done) @v:0.1
    S1_Authoring // hand-written contract layer (done)
        OaSkill // PAO_skills/pao-oa/SKILL.md + references/ (done)
            # references: reconcile.md, publish.md, collect-validate.md, recover-maintain.md
            # criteria: SKILL.md <= ~70 lines; every action row names exactly one reference doc; forbidden-actions kept in SKILL.md
        LwarSkill // PAO_skills/pao-lwar/SKILL.md + references/ (done)
            # references: register.md, adp-loop.md, execute-complete.md, lifecycle.md
            # note: adp-loop.md absorbs the adp-contract (mailbox layout, exit codes, lease alignment, recovery)
            # criteria: same as OaSkill; ADP absolute rules (idle re-run, no unapproved identity, submit-before-return) stay in SKILL.md
    S2_RuntimeBundling // generated layer (done)
        BuildSkillsCmd // pao_cli `build-skills`: copy pao_runtime+scripts into both skills, schemas into pao-lwar (done)
            # default target <repo>/PAO_skills (package parents[2]); --target override; removes-then-copies each generated dir
        InitialBuild // run build-skills once; generated trees committed as distribution artifacts (done) @dep:BuildSkillsCmd
    S3_SyncGates // drift prevention (done) @dep:InitialBuild
        HashGateTests // byte-equality: plugin pao_runtime/scripts/schemas vs bundled copies (same file set, same bytes) (done)
        ContractGateTests // frontmatter name==folder, lowercase kebab; references exist; no CLAUDE_PLUGIN_ROOT/PAO_HOME/python -m in PAO_skills (done)
    S4_DocsSync // README operation channel B -> PAO_skills; AGENTS.md; TechSpec §15 (done)
    S5_Verify // gates (done) @dep:S3_SyncGates
        FullSuite // unittest discover green (done)
        Compile // py_compile incl. generated copies (done)
        FunctionalSmoke // python PAO_skills/pao-oa/scripts/oa.py status --root <tmp> works with no env (done)
        SkillLoadSmoke // headless claude with skills dir containing pao-oa: /pao-oa resolves, agent states $PAO_SKILL (done)
            # method: CLAUDE_CONFIG_DIR sandbox first; fallback = temporary copy into ~/.claude/skills with cleanup, reported honestly
```

## PPR — non-obvious nodes

```python
def BuildSkillsCmd(target: Path = repo / "PAO_skills") -> None:
    """Materialize the generated layer of both standalone skills."""
    # input: canonical trees under PAO_plugin/
    # process:
    #   for skill in ("pao-oa", "pao-lwar"):
    #       replace(target / skill / "pao_runtime", plugin / "pao_runtime")   # rmtree + copytree, skip __pycache__
    #       replace(target / skill / "scripts",     plugin / "scripts")
    #   replace(target / "pao-lwar" / "schemas", plugin / "skills/lwar-runtime/schemas")
    #   emit({"event": "skills_built", ...})
    # acceptance_criteria:
    #   - authored files (SKILL.md, references/) are never touched
    #   - idempotent: second run produces identical trees
```

## Design review resolutions (red team, 2026-07-16 — 14 findings)

- F1 (High): the plugin-agnostic grep gate scopes to **authored files only** (SKILL.md + references/*.md) — the generated runtime legitimately contains `PAO_HOME` strings. F13 (bundle pao_cli exclusion) rejected: excluding it would break the `pao.py` wrapper and the `pao info` diagnostic needed by F7; whole-tree identity keeps the hash gate simple.
- F2 (High): `build-skills` copies with `ignore_patterns("__pycache__", "*.pyc")`; the hash gate excludes the same on both sides. Runtime-generated caches inside PAO_skills are already git-ignored.
- F3 (High): placeholder renamed `$PAO_SKILL` → **`<PAO_SKILL>`** (angle brackets, like `<identity_file>`): unresolved paste fails loudly in a shell instead of silently expanding empty. SKILL.md §0 states it is a documentation placeholder to be replaced with the absolute folder containing the loaded SKILL.md, always quoted.
- F4 (High): "Never approve success from `exit_code=0` alone" and "do not rewrite failed validation as success" are **promoted to pao-oa SKILL.md Forbidden Actions** (always loaded), duplicated in collect-validate.md.
- F5/F6 (Medium): `build-skills` refuses to run unless the target skill's authored `SKILL.md` exists (never scaffolds), verifies canonical sources exist, and only replaces the named generated dirs. Gate test: authored bytes unchanged across a build; second build byte-identical (idempotent); refusal path tested.
- F7 (Medium): functional smoke asserts `pao info` `package_dir` resolves **inside the bundle** (foreign cwd, no PYTHONPATH) — proves the bundled runtime executed, not an ambient one.
- F8 (Medium): hash gate = symmetric file-set equality + per-file byte comparison (read bytes, both directions).
- F9 (Medium): pao-lwar SKILL.md absolute rule 1 requires reading `references/adp-loop.md` **in full before the first watch slice** (exit codes, lease alignment, stale-identity rejection are pre-loop knowledge), not lazily per event.
- F10 (Medium): docs name **three channels** explicitly — A: plugin (`/pao:*`, `${CLAUDE_PLUGIN_ROOT}`), B: standalone PAO_skills (`/pao-oa`, `<PAO_SKILL>`, PAO_ROOT only), C: thin contract copy + `PAO_HOME` (`pao install-skills`). Coexistence note: `/pao:oa` and `/pao-oa` do not collide but users should pick one channel per machine.
- F11 (Low): gates compare raw bytes; `.gitattributes` `* text=auto eol=lf` keeps tracked copies LF cross-platform.
- F12 (Low): every example stays quoted with forward slashes.
- F14 (Low): SkillLoadSmoke skips are loud — outcome (verified / fallback used / unverified) is reported explicitly, never a silent pass.

## Risks / falsification

- R1: AI skips the routing table and acts without reading the reference doc → mitigated by an absolute rule in SKILL.md ("read the named reference before performing the action") and SkillLoadSmoke; residual risk accepted (PG enforcement caveat).
- R2: generated copies drift from canonical → HashGateTests make drift a red suite; `build-skills` is the only sanctioned writer.
- R3: `$PAO_SKILL` convention fails in some runtime (skill loaded without a known path) → falsified if SkillLoadSmoke cannot state the directory; fallback documented: user replaces `$PAO_SKILL` with the copy location.
- R4: three runtime copies inflate the repo → measured cost ~60KB per copy; accepted by approval.
- R5: `install-skills` (plugin-contract copier) now coexists with the PAO_skills channel and may confuse — out of scope to change; README wording must disambiguate.
