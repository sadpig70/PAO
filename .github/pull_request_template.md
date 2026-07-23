## Problem and intended outcome

<!-- Describe the problem, why it matters, and the observable outcome. -->

## Changed files and contracts

<!-- List the files changed and any runtime, schema, skill, or documentation contract affected. -->

## Verification performed

<!-- Include exact commands and results. Targeted tests do not replace the full suite. -->

- [ ] `python -m compileall -q .agents/skills/pao-lwar .agents/skills/pao-oa tests tools`
- [ ] `python -m unittest discover -s tests -q`
- [ ] `python tools/sync_bundles.py --check`
- [ ] `git diff --check`

## Risk assessment

<!-- Address compatibility, recovery/durability, portability, and security/authority risk. State "none identified" where appropriate. -->

## Source-boundary checklist

- [ ] Runtime changes were made only in the LWAR master, or this is not applicable.
- [ ] The generated OA mirror was synchronized after runtime changes, or this is not applicable.
- [ ] Tracked text is English and examples use repository-relative paths.
- [ ] No credentials, machine-specific paths, `.pao/` state, or generated runtime data are included.
- [ ] This pull request is current with `main` and is ready for both required CI jobs.
