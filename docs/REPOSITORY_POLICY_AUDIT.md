# Repository Policy Audit

The repository policy audit detects drift between the checked-in protection
contract and GitHub's live `main` branch settings.

## Contract

`.github/repository-policy.json` is the reviewable policy source. It requires:

- strict status-check evaluation
- exactly three GitHub Actions app-bound checks:
  - `PR Evidence`
  - `Verify (ubuntu-latest)`
  - `Verify (windows-latest)`
- administrator enforcement

The audit fails on missing or additional checks, changed `app_id` values,
disabled strict evaluation, disabled administrator enforcement, malformed
policy, unreadable live state, or an API error.

## Execution

`.github/workflows/repository-policy-audit.yml` runs:

- when the policy implementation reaches `main`
- every day at 03:17 UTC
- on manual dispatch

A failed scheduled run is the drift alert. The job uses only `contents: read`;
the GitHub token is used to read current branch protection and is never written
to disk or output.

For a local authenticated audit:

```bash
GITHUB_TOKEN="$(gh auth token)" python tools/verify_repository_policy.py \
  --repository owner/repository
```

For deterministic offline diagnosis, save a branch-protection API response and
run:

```bash
python tools/verify_repository_policy.py \
  --live-file path/to/branch-protection.json
```

## Recovery

Treat the checked-in policy as the intended state. Restore GitHub protection to
that state, manually dispatch the workflow, and require a successful audit.
Change the policy file only through a reviewed pull request when the intended
governance contract itself changes.
