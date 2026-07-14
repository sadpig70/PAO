# WORKPLAN-PAOPortable

> Source design: `_workspace/DESIGN-PAOPortable.md` (design-reviewed 2026-07-15, Critical 0 / High 0)
> Mode: execute | All code, docs, and tests in English.

## POLICY

- backward_compat: all 30 existing tests pass unmodified; explicit --root always wins over PAO_ROOT
- no_env_pollution: verification must not pip-install into the user's environment
- schema_changes: none
- docs_sync: skills, AGENTS.md, README updated in the same change set
- max_verify_cycles: 2

## Nodes

```text
PAOPortable // PAO workspace decoupling (in-progress) @v:0.1
    P1_RootResolution // bus root resolution independent of cwd (in-progress)
        ResolveRootHelper // common.resolve_root: --root > PAO_ROOT > cwd (in-progress)
        CliRootDefaults // oa_cli/lwar_cli/adp_watch default --root via resolver (in-progress) @dep:ResolveRootHelper
    P2_Packaging // pip-installable runtime (in-progress)
        PyprojectManifest // pyproject.toml pao-runtime 0.3.0 (in-progress)
        ConsoleScripts // pao, pao-oa, pao-lwar, pao-adp-watch entry points (in-progress) @dep:PyprojectManifest
    P3_SkillDistribution // global skill/contract distribution (in-progress)
        PaoCli // pao_runtime/pao_cli.py — info + install-skills (in-progress)
        SkillDocsUpdate // operation-mode notes in both SKILL.md files (in-progress)
        AgentsDualMode // AGENTS.md development/operation split (in-progress)
        ReadmeUpdate // Installation and operation guide (in-progress)
    P4_CwdGuard // cross-workspace safety (in-progress)
        CwdValidation // send rejects non-existent task cwd (in-progress)
    P5_Tests // portability suite (in-progress)
        RootResolutionTests // env fallback, precedence, foreign-cwd run (in-progress) @dep:P1_RootResolution
        InstallerTests // install-skills copy verification (in-progress) @dep:PaoCli
        PackagingTests // entry-point importability from pyproject (in-progress) @dep:P2_Packaging
        CwdGuardTests // missing cwd rejection (in-progress) @dep:CwdValidation
```

## Verification

- `python -m unittest discover -s tests -v` — all suites green (30 legacy + new)
- `python -m py_compile pao_runtime/*.py scripts/*.py tests/*.py`
- foreign-cwd subprocess run proves workspace independence without pip install
