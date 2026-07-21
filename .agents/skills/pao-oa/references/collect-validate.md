# OA Reference — Collection and Validation

Replace `<PAO_SKILL>` with this skill's folder (SKILL.md §0).

## Commands

```bash
python "<PAO_SKILL>/scripts/oa.py" collect
python "<PAO_SKILL>/scripts/oa.py" collect --archive
python "<PAO_SKILL>/scripts/oa.py" validate --task-id TASK_ID
python "<PAO_SKILL>/scripts/oa.py" validate --task-id TASK_ID --record --decision accepted --reason "verified"
python "<PAO_SKILL>/scripts/oa.py" workflow-status --workflow-id WORKFLOW_ID
```

## Rules

- Every ingress result is schema-validated before interpretation. `collect` quarantines invalid contracts, stale generation/attempt/claim-token provenance, and duplicates; quarantined results are never auto-approved.
- The attempt fence is strict: results echo both `attempt` and `claim_token`; `collect` matches them against the exact archived claim. `recover` and `dead --requeue` bump the attempt, so a late result from a superseded claim is rejected.
- Every result is a **terminal submission, not a success claim** — only `status=succeeded` results are acceptance candidates; `failed`, `blocked`, `cancelled`, `interrupted`, `timed_out`, and `protocol_error` results are collected for the record and routed to recovery or reporting.
- `collect` also verifies artifact provenance: each artifact object's content-addressed snapshot (`var/artifacts/<sha256>`) is size-checked then re-hashed; any mismatch quarantines the result as `artifact_tampered`. Legacy string artifacts carry no snapshot and skip this check. Provenance certifies the bytes the LWAR submitted — it detects post-submit mutation, it does **not** make the artifacts trustworthy.
- `collect` commits the ledger before optional result archival, then reconciles archived results on later passes. A crash on either side of the move is therefore repairable.
- `validate` reports mechanical checks; **semantic verification remains OA's judgment**. Record it explicitly with `--decision accepted|rejected|undecidable --reason ...`. `accepted` is refused when mechanical checks fail. `validate --record` is mutating and requires the writer lease; plain `validate` stays observer-safe.
- Never approve success from `exit_code=0` alone. Validate `completion_criteria`, `evidence` (commands run, tests passed/failed), `artifacts`, and actual test results.
- Do not rewrite failed validation as success. A failed or unverifiable result goes back through recovery ([recover-maintain.md](recover-maintain.md)) or is reported honestly.
- `workflow-status` aggregates ledger state per workflow; use it before publishing `depends_on` successors.

## Closeout decision tree (after `validate`)

`validate` does not accept or reject on its own — it emits a `verdict`
(`ready_for_oa_review` when the mechanical checks pass, else `attention_required`)
with the `completion_criteria` left as `manual_check_required`. You, the OA, decide
the closeout:

```text
validate --task-id T
├─ verdict = attention_required
│    → the mechanical checks failed (wrong status, missing evidence/artifacts,
│      exit_code ↔ status mismatch). Do NOT accept. Route to recovery
│      (recover-maintain.md) or report the failure honestly to the user.
└─ verdict = ready_for_oa_review
     → mechanical checks passed; now apply YOUR semantic judgment against each
       completion_criterion using the evidence/artifacts.
       ├─ criteria genuinely met → accept: record it with
       │    `validate --record --decision accepted --reason "..."`
       │    (persists the ValidationDecision) and report the task done.
       ├─ criteria NOT met despite green mechanics → treat as a failure: recover
       │    or report; never rewrite it as success.
       └─ genuinely undecidable by you → surface to the user with the evidence;
            do not fabricate an acceptance.
```

Acceptance is the ledger's `completed` state plus a recorded semantic decision of
`accepted`. A completed result without that decision cannot satisfy a dependency.
