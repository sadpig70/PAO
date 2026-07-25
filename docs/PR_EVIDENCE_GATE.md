# PR Evidence Gate

The `PR Evidence Evaluator` check turns the repository's pull-request template
into an enforced evidence contract.

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

The workflow has only `contents: read`. The `pull_request_target` job's own
`PR Evidence Evaluator` result is the required check. It validates the event's
immutable pull-request body snapshot and exits nonzero on any contract error.
No custom check is published to the synthetic merge commit.

## Required protection

The protected `main` branch binds `PR Evidence Evaluator` to the GitHub Actions
app alongside both platform verification jobs. Strict protection still
requires the branch to be current with `main`. Avoid publishing a partial set
of required statuses to the synthetic merge commit: once that commit has any
status, GitHub evaluates the required set there and otherwise-valid head checks
no longer satisfy the gate. Administrators must not bypass this requirement.

## Recovery

The workflow runs for opened, synchronized, reopened, and edited pull requests.
To recover a failed check:

1. fill every required narrative section with concrete evidence
2. check every required verification and source-boundary item
3. edit the pull-request body
4. wait for the replacement `PR Evidence Evaluator` check

Do not use a retry to conceal missing evidence. A retry against unchanged
content should fail again.
