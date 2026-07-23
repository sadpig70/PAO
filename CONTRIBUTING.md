# Contributing to PAO

PAO uses a protected `main` branch. Every tracked change must travel through a
feature branch and pull request, then pass the Ubuntu and Windows verification
jobs before merge.

## Source of truth

- `.agents/skills/pao-lwar` is the runtime master.
- Do not edit the generated runtime layer under `.agents/skills/pao-oa`
  directly.
- When `pao_runtime/`, `scripts/`, or `schemas/` changes in the LWAR bundle,
  run `python tools/sync_bundles.py` to regenerate the OA mirror.
- Authored files such as each skill's `SKILL.md` and references remain
  independently editable.
- Keep tracked documentation, code comments, and skill content in English.
- Use repository-relative examples. Do not commit machine-specific paths,
  credentials, `.pao/` bus state, or generated runtime data.

## Development workflow

1. Update local `main` from `origin/main`.
2. Create a focused feature branch. Do not commit directly to `main`.
3. Make the smallest coherent change and update its tests or documentation.
4. If runtime master files changed, synchronize the bundles.
5. Run the local verification gates.
6. Push the feature branch and open a pull request against `main`.
7. Wait for all required GitHub Actions jobs:
   - `PR Evidence`
   - `Verify (ubuntu-latest)`
   - `Verify (windows-latest)`
8. Merge without an administrator bypass, then delete the feature branch.
9. Confirm the merge commit's `main` workflow succeeds and the local worktree
   is clean.

The branch protection rule is strict: a pull request must be current with
`main`, and every required check must come from the GitHub Actions app.
The scheduled repository policy audit detects live drift from that contract;
see `docs/REPOSITORY_POLICY_AUDIT.md`.

## Local verification

Run these commands from the repository root:

```bash
python -m compileall -q .agents/skills/pao-lwar .agents/skills/pao-oa tests tools
python -m unittest discover -s tests -q
python tools/sync_bundles.py --check
git diff --check
```

The full suite is the release gate. A targeted test may shorten iteration, but
it does not replace the full suite before merge.

## Pull requests

Keep each pull request reviewable and state:

- the problem and intended outcome
- the files and contracts changed
- the exact verification performed
- any compatibility, recovery, or portability risk

Do not hide a failing check with a retry-only workaround. Reproduce the
failure, fix the cause or make the assertion deterministic, and let both
platform jobs validate the correction.

`PR Evidence` derives its required headings and checkboxes from
`.github/pull_request_template.md`. It fails closed on missing, duplicate,
out-of-order, placeholder-only, or unchecked evidence. Editing the PR body
triggers validation again.

## Windows development

Use `pwsh -NoProfile` for PowerShell 7 commands. Do not use Windows PowerShell
5.1. Invoke Python as `python` from `PATH`; do not bind documentation or scripts
to a machine-specific interpreter location.
