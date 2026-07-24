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
the default workflow token is used only for checkout and is not persisted.
GitHub does not grant that token access to repository-administration endpoints,
so the audit reads protection with the encrypted
`REPOSITORY_POLICY_AUDIT_TOKEN` Actions secret.

Provision that secret with a fine-grained token limited to this repository and
these read-only permissions:

- Administration: read
- Metadata: read

The token is passed only to the audit process and is never written to disk or
output. A missing, expired, or underprivileged secret fails closed.

## Credential lifecycle

`.github/repository-policy-credential.json` classifies the token only by its
non-secret GitHub prefix and enforces a repository-owned `not_after` ceiling.
The audit never prints or persists the token.

The preferred credential is a `github_pat_` fine-grained PAT. The existing
`gho_` OAuth bootstrap is accepted only through 2026-07-31 UTC and emits a
rotation warning on every audit. After that ceiling it fails closed before any
GitHub API request. The fine-grained entry is also time-bounded, so each
rotation must update the reviewed ceiling without exceeding the provider-side
expiration.

GitHub does not expose a token's personal expiration metadata to this workflow.
The checked-in ceiling is therefore an additional local upper bound, not a
replacement for GitHub's own expiration and revocation.

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
