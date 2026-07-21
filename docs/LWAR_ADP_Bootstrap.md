# PAO Skill-Only Bootstrap

This file is intentionally not an operating prompt. The two role skills are
PAO's sole operating prompts and canonical runtime instructions:

- `.agents/skills/pao-oa/SKILL.md`
- `.agents/skills/pao-lwar/SKILL.md`

Start the runtimes on the same local bus and give each only its role instruction:

```text
Read <absolute-path>/pao-oa/SKILL.md and act as the PAO OA.
```

```text
Read <absolute-path>/pao-lwar/SKILL.md and act as a PAO LWAR.
```

The role skill resolves its own folder, runs pre-flight, reads its bundled
references, and performs bootstrap. Do not copy a second registration, watcher,
completion, or supervision prompt from repository documentation; duplicated
commands drift from the bundled contract.

The only shared deployment prerequisite is one single-host local bus. Launch
both sessions from the same project directory to use `<cwd>/.pao`, or configure
the same `PAO_ROOT` for both before invoking their skills.
