# PR Evidence Gate

The `PR Evidence` check turns the repository's pull-request template into an
enforced evidence contract.

## Contract

`.github/pull_request_template.md` is the single contract source. The validator
derives the required H2 sections and checkbox labels directly from that file.
It fails closed when evidence is:

- empty or missing
- duplicated or out of order
- still only an HTML template comment
- missing a contract checkbox
- carrying an unchecked contract checkbox

The PR body is parsed as inert Markdown. No body content is evaluated,
interpolated, or executed.

## Trust boundary

`.github/workflows/pr-evidence.yml` runs on `pull_request_target` and checks out
the exact base commit, not the PR branch. A pull request therefore cannot pass
the gate by changing its own validator or workflow.

The workflow has only:

- `contents: read`
- `pull-requests: read`
- `checks: write`

After validation, the trusted base code reads current PR metadata from the
Pulls API, then publishes completed `PR Evidence` checks to `head.sha` and the
synthetic `merge_commit_sha`. Publishing both binds the evidence to the commit
selected by strict branch protection. A missing live merge SHA fails closed.
Each check records either `success` or actionable validation errors. Failure
to read, parse, validate, or publish leaves the pull request blocked.

## Required protection

The protected `main` branch binds `PR Evidence` to the GitHub Actions app
alongside both platform verification jobs. Strict protection evaluates the
synthetic merge commit, so publishing only to the head commit is insufficient.
Administrators must not bypass this requirement.

## Recovery

The workflow runs for opened, synchronized, reopened, and edited pull requests.
To recover a failed check:

1. fill every required narrative section with concrete evidence
2. check every required verification and source-boundary item
3. edit the pull-request body
4. wait for the replacement `PR Evidence` check

Do not use a retry to conceal missing evidence. A retry against unchanged
content should fail again.
