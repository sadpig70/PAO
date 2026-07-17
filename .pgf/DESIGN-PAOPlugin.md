# DESIGN-PAOPlugin

> Goal: package PAO — OA/LWAR skills, runtime code, scripts, schemas, references, operation docs — as a single installable Claude Code plugin, while keeping the manual-copy channel for foreign (non-Claude) runtimes.
> Decisions adopted (reviewed 2026-07-16): repo root = plugin (no build artifact); foreign-runtime channel retained via `pao install-skills` / plain copy.

## Constraints

- Plugin spec facts (verified against official docs, Claude Code v2.1.207+):
  - `.claude-plugin/plugin.json` manifest; `skills/` and `commands/` are auto-recognized root dirs.
  - Plugin skills are namespaced `pao:oa-runtime`, commands `/pao:lwar-register`.
  - `${CLAUDE_PLUGIN_ROOT}` is substituted in skill/command content; it changes on every plugin update, so no persistent state may live under it.
  - Plugins cannot run install steps — satisfied: runtime is stdlib-only, wrappers bootstrap `sys.path`.
- Bus root (`PAO_ROOT` resolution) is orthogonal and unchanged: `--root` > `PAO_ROOT` > cwd.
- Foreign runtimes never see `${CLAUDE_PLUGIN_ROOT}` substitution → skills must document both invocation forms.
- Version is declared in three places after this change (pyproject.toml, `pao_runtime.__version__`, plugin.json) → must be gated by a sync test.

## Gantree

```text
PAOPlugin // PAO single-plugin packaging (done) @v:0.4
    P1_Manifest // plugin identity and distribution metadata (done)
        PluginJson // .claude-plugin/plugin.json — name pao, version 0.4.0 (done)
            # criteria: claude plugin validate passes; name kebab-case; version == pao_runtime.__version__
        MarketplaceJson // .claude-plugin/marketplace.json — single entry, source "./" (done) @dep:PluginJson
            # criteria: validate passes; enables `/plugin marketplace add <repo>`
    P2_SkillRelocation // canonical skills move to plugin layout (done)
        MoveSkills // git mv .agents/skills/{oa-runtime,lwar-runtime} -> skills/ (done)
            # note: pg/pgf/pgxf stay in .agents/skills (dev-only, not shipped as plugin skills)
        SkillInvocationForms // both SKILL.md: add ${CLAUDE_PLUGIN_ROOT} form beside $PAO_HOME form (done) @dep:MoveSkills
            # criteria: plugin session resolves scripts without PAO_HOME; copied skill still valid for foreign runtimes
    P3_RuntimeBundle // runtime ships inside the plugin as-is (done)
        InstallSkillsSource // pao_cli default_skills_source -> <repo>/skills; help/error text updated (done) @dep:MoveSkills
        VersionBump // 0.3.0 -> 0.4.0 in pyproject + __init__ (done)
    P4_Commands // thin command aliases for documented UX (done)
        CmdLwarRegister // commands/lwar-register.md -> /pao:lwar-register [number] (done) @dep:MoveSkills
        CmdOa // commands/oa.md -> /pao:oa [action] (done) @dep:MoveSkills
    P5_DocsSync // same-change-set documentation updates (done)
        ClaudeMdPaths // CLAUDE.md role routing paths -> skills/ (done) @dep:MoveSkills
        AgentsMdModes // AGENTS.md: dev-mode skill location + plugin deployment mode (done) @dep:MoveSkills
        ReadmeInstall // README: plugin install as primary channel, manual copy for foreign runtimes (done) @dep:P1_Manifest
        TechSpecModes // docs/PAO_TechSpec.md §15 deployment modes gains plugin mode (done) @dep:P1_Manifest
    P6_Tests // packaging gates (done)
        DynamicVersionAsserts // test_portability version asserts read pao_runtime.__version__ (done) @dep:VersionBump
        InstallerPathUpdate // InstallerTests source refs -> skills/ (done) @dep:InstallSkillsSource
        PluginPackagingTests // new tests/test_plugin_packaging.py (done) @dep:PluginJson,MarketplaceJson,CmdLwarRegister
            # process:
            #   plugin.json parses, name == "pao", version == __version__ == pyproject version
            #   marketplace.json parses and lists plugin "pao" with source "./"
            #   skills/{oa-runtime,lwar-runtime}/SKILL.md exist; lwar schemas + references present
            #   commands/{lwar-register,oa}.md exist with frontmatter description
            #   no ${CLAUDE_PLUGIN_ROOT} literal inside .agents/skills (dev skills must stay harness-agnostic)
    P7_Verify // cross-check gates (done) @dep:P6_Tests
        FullSuite // python -m unittest discover -s tests — all green (done)
        Compile // py_compile over pao_runtime, scripts, tests (done)
        PluginValidate // claude plugin validate . (done)
        PluginSmoke // headless claude -p --plugin-dir: observe ${CLAUDE_PLUGIN_ROOT} substitution (done) @dep:P2_SkillRelocation
        ForeignSmoke // foreign-cwd scripts/pao.py info without PYTHONPATH (done)
```

## PPR — non-obvious nodes

```python
def SkillInvocationForms(skill_md: Path) -> None:
    """Document dual invocation without breaking either channel."""
    # input: skills/{oa,lwar}-runtime/SKILL.md invocation-forms section
    # process:
    #   plugin_form = 'python "${CLAUDE_PLUGIN_ROOT}/scripts/<cli>.py"'  # substituted only by Claude Code
    #   manual_form = 'python "$PAO_HOME/scripts/<cli>.py"'              # foreign runtimes and manual installs
    #   state precedence: plugin form when installed as plugin; PAO_HOME otherwise
    # acceptance_criteria:
    #   - neither form is removed; both are labeled with when-to-use
    #   - no other section of either SKILL.md changes semantics
```

## Design review resolutions (red team, 2026-07-16 — 12 findings)

- F1 (Critical): every executable example in both SKILL.md bodies switches from `python -m pao_runtime.*` to `python "$PAO_HOME/scripts/*.py"`; `-m` and pip forms remain as inline prose alternatives in the invocation section only. Gate: test asserts no `python -m pao_runtime` command lines in shipped skills.
- F2 (Critical): section 0 of each skill gains a plugin-root reveal line containing the literal `${CLAUDE_PLUGIN_ROOT}` token — substituted to the absolute path when loaded from a plugin, read as documentation otherwise. P7 gains PluginSmoke (headless `claude -p --plugin-dir`) to observe substitution; result reported honestly either way. `$PAO_HOME` remains the documented fallback, so plugin mode degrades to one env var, never to broken.
- F3 (High): ship all 8 command aliases (`oa`, `lwar-register`, `lwar-adp`, `lwar-status`, `lwar-on`, `lwar-drain`, `lwar-off`, `lwar-unregister`); lwar skill command table gains the `/pao:` namespace note.
- F4 (High): `default_skills_source` probes `<repo>/skills` then legacy `<repo>/.agents/skills`; missing-source errors name the canonical `skills/` location.
- F5 (High): AGENTS.md dev-mode wording covers the split (`skills/` = shipped contracts, `.agents/skills/` = dev tooling); CLAUDE.md load paths updated.
- F6/F7 (Medium): all three hard-coded version asserts (test_portability L57/L89/L156) and both installer path sites (L105/L122) are in scope.
- F8 (Medium): PluginPackagingTests asserts the old `.agents/skills/{oa,lwar}-runtime` paths are GONE (single_source gate).
- F9 (Medium): resolved by F1+F2 — one canonical `$PAO_HOME` convention with an in-skill substituted path reveal.
- F10 (Medium): P5 gains docs/LWAR_ADP_Bootstrap.md (deployment-mode note) and README test-count refresh to the actual final count.
- F11 (Low): git mv executed as two explicit commands via Git Bash, no brace expansion.
- F12 (Low): new tests read the pyproject version via regex, not tomllib.

## Revision R2 (2026-07-16, user-approved variant A): PAO_plugin subdirectory as plugin root

Rationale: lean install package (no tests/.pgf/.agents in the plugin cache), removes the root-CLAUDE.md validate warning. Single source preserved — everything moves, nothing is copied.

```text
PAOPluginR2 // PAO_plugin/ as plugin root, single source (done) @v:0.4
    R2_Move // git mv skills, commands, scripts, pao_runtime, docs, plugin.json, pyproject into PAO_plugin/ (done)
        # note: pyproject moves too so `pip install -e $PAO_HOME` keeps working (PAO_HOME = PAO_plugin)
        # note: PAO_plugin/README.md created (pyproject readme field must resolve)
    R2_Marketplace // root marketplace.json source "./" -> "./PAO_plugin" (done) @dep:R2_Move
    R2_TestHarness // pao_helpers gains PLUGIN path + sys.path/PYTHONPATH bootstrap; path asserts updated (done) @dep:R2_Move
        # criteria: `python -m unittest discover -s tests` green from repo root without pip install
    R2_DocsSync // CLAUDE.md, AGENTS.md, README, TechSpec, Bootstrap paths -> PAO_plugin/ (done) @dep:R2_Move
    R2_Verify // full suite + py_compile + validate --strict on PAO_plugin + plugin smoke vs PAO_plugin (done) @dep:R2_TestHarness
        # criteria: strict validate now clean (no root CLAUDE.md in plugin dir); smoke shows substituted path D:/PAO/PAO_plugin
```

Invariants: wrapper scripts stay correct unchanged (`parents[1]` = PAO_plugin contains pao_runtime); `PAO_HOME` now denotes the PAO_plugin directory in every doc; bus semantics untouched.

## Risks / falsification

- R1: manifest field set drifts from docs → gate = `claude plugin validate`.
- R2: `${CLAUDE_PLUGIN_ROOT}` not substituted inside fenced bash blocks of SKILL.md → fallback = PAO_HOME form still documented; detection = post-install smoke (out of scope for this change, recorded as follow-up).
- R3: version skew across three declarations → gate = PluginPackagingTests sync assert.
- R4: foreign-runtime copies break if skills reference plugin-only paths outside the invocation-forms section → gate = review checklist on skill diff.
