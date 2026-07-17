# WORKPLAN-PAOPlugin

> Source design: `.pgf/DESIGN-PAOPlugin.md` (design review pending — execution starts only after Critical/High findings are resolved)
> Mode: execute | All code, docs, and tests in English.

## POLICY

- behavior_compat: all runtime behavior (bus, CLIs, root resolution) unchanged; this is packaging + docs only
- test_updates: existing tests may be edited only where they hard-code the moved skill path or the version string; all suites must end green
- single_source: oa-runtime and lwar-runtime exist exactly once in the repo (skills/); no copies left in .agents/skills
- foreign_channel: manual copy / `pao install-skills` stays a documented, working channel for non-Claude runtimes
- docs_sync: CLAUDE.md, AGENTS.md, README.md, TechSpec updated in the same change set
- no_commit: no git commit/push without explicit user command
- max_verify_cycles: 2

## Execution order

1. P2_SkillRelocation (MoveSkills → SkillInvocationForms)
2. P3_RuntimeBundle (InstallSkillsSource, VersionBump)
3. P1_Manifest (PluginJson → MarketplaceJson)
4. P4_Commands
5. P5_DocsSync
6. P6_Tests
7. P7_Verify

Node tree, statuses, and PPR live in `DESIGN-PAOPlugin.md`; this file tracks policy and order. Status snapshots go to `status-PAOPlugin.json`.
