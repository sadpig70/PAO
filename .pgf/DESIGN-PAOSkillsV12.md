# DESIGN — PAOSkillsV12 (authority enforcement, provenance, validation record)

> Date: 2026-07-18 | Base: `201eed0` | Canonical: `PAO_skills/pao-lwar` master → mirrors
> Scope: upgrade-plan Phase 3 — turn advisory `permissions` into enforced bounds,
> add artifact sha256 provenance with tamper detection, persist ValidationDecision.
> Out of scope: sandboxing the agent itself (the runtime enforces what deterministic
> code can enforce: publication guards, claim guards, submission guards, collection
> verification); network permission stays declarative.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **cwd deny-set = bus control surfaces (`mailbox/`, `var/`, `control/`) + the runtime bundle dir — NOT the whole bus root** | The invariant to protect is control-plane integrity. Operators legitimately keep workspaces under the bus dir (both soaks did); the whole-root ban would also break every existing test that uses `cwd=root`. Enforced at three layers: send (authoritative), watcher claim (defense in depth vs hand-planted tasks), complete (artifact roots). |
| D2 | **Artifacts become objects `{path, sha256, size_bytes}` computed by the `complete` CLI, never by the agent** | The deterministic CLI hashing the real files removes fabrication; strings stay accepted as input (resolved relative to task cwd) and legacy string artifacts in old results skip verification (optional-first, same rollout pattern as the attempt fence). Missing declared artifact → complete fails (scenario 20). |
| D3 | **Artifact bounds = task cwd + permissions.write roots** | Symlink escapes are caught because `Path.resolve()` follows links before the containment check. Optional `permissions.max_artifact_bytes` enforced at complete. |
| D4 | **collect re-hashes accepted results' artifact objects; mismatch/missing → quarantine `artifact_tampered`** | Scenario 11 (file changed after submit) is caught at the trust boundary where results become canonical. |
| D5 | **`validate` persists a structured ValidationDecision into the ledger entry (`validation` key) and re-verifies artifact hashes; result originals stay immutable** | Decision separated from the immutable ResultContract per upgrade-plan P1-9; `validate` thereby becomes a mutating command → takes the writer lease. |
| D6 | **send default permissions change from `{"read":[root],"write":[]}` to `{"read":[cwd],"write":[cwd]}`** | Bus-root read as a default contradicts the new boundary; cwd-scoped default is the least surprising. Permission entries resolving inside the deny-set are rejected at send. |
| D7 | Watcher claim-guard reuses `_reject_task` (task → `failed/` + error file) | Same mechanism and same ledger-visibility trade-off as today's stale-identity/invalid-json rejections; OA sees the stuck `published` entry via workflow-status. |

## Gantree

```
PAOSkillsV12 // enforced bounds + provenance + validation record (in-progress) @v:1.2
    DesignReview // 1 red-team agent (security + feasibility lenses) (in-progress)
    AuthorityGuards // (designing) @dep:DesignReview
        # common.py: path_within(), runtime_bundle_root(), authority_denied_reason(cwd, root)
        # oa_cli.send: canonical cwd; deny-set rejection; permissions shape validation
        #   (read/write: list[str], network: bool, max_artifact_bytes: optional int>0);
        #   permission entries inside deny-set rejected; new default (D6)
        # transport.claim_task: deny-set + cwd-exists guard -> _reject_task("authority_violation:*")
        # criteria: scenario 14-analogue (cwd in mailbox/var/control) rejected at send AND at claim
    ArtifactProvenance // (designing) @dep:DesignReview
        # lwar_cli.complete: resolve each artifact against cwd; must exist, within
        #   cwd|write roots, within max_artifact_bytes; emit {path, sha256, size_bytes}
        # oa_cli.collect: accepted results with object artifacts re-hashed;
        #   mismatch/missing -> quarantine "artifact_tampered"
        # schemas/result.schema.json: artifacts items = object|string(legacy)
        # criteria: scenarios 11 (tamper->quarantine) and 20 (missing->complete fails)
    ValidationDecision // (designing) @dep:ArtifactProvenance
        # oa_cli.validate: + ensure_oa_writer; artifact re-verification; persist
        #   entry["validation"] = {schema_version, verdict, checks, criteria,
        #   artifact_verification, decided_by (PAO_OA_ID), decided_at}
        # ledger.record_validation(task_id, workflow_id, decision)
        # schemas/validation-decision.schema.json (new)
    DocsSync // (designing) @dep:AuthorityGuards,ArtifactProvenance,ValidationDecision
        # skills refs: execute-complete (artifact objects, bounds), publish (deny-set,
        #   defaults), collect-validate (artifact_tampered, decision record)
        # plugin contracts: oa-runtime SKILL bullets, adp-contract Result Contract para
        # task payload remains untrusted data — restated where bounds are described
    MirrorAndTests // (designing) @dep:DocsSync
        # sync_bundles --to-plugin; installed ~/.claude/skills copies refreshed
        # tests/test_skills_v12.py: send deny-set reject; claim guard rejects planted
        #   task; complete hashes artifacts / fails on missing / fails on escape
        #   (.. traversal; symlink case skipUnless platform allows); collect quarantines
        #   tampered artifact; legacy string artifacts pass; validate persists decision
        #   + verifies hashes; default-permissions shape
        # full suite green; plugin validate --strict
```

## Design Review — Round 1 (applied)

11 findings; dispositions (all P1s change the design):

| ID | Finding | Disposition |
|---|---|---|
| R1 (P1) | collect cannot re-hash: nothing carries cwd to collection | Artifact objects store the **resolved absolute path**; verification never needs cwd (see R2). |
| R2 (P1) | Re-hashing the live workspace file falsely quarantines legitimately-reused workspaces | **Snapshot store**: `complete` copies each artifact in one hash-while-copy pass into content-addressed `var/artifacts/<sha256>`; objects carry `snapshot` (root-relative). collect/validate verify the immutable snapshot, never the live file. Post-submit workspace changes become irrelevant by construction. |
| R3 (P1) | Writer-gating `validate` breaks the documented observer pattern | `validate` stays read-only; new `validate --record` persists the decision (takes the lease only after the early returns). SKILL §1 lists `validate --record` as mutating. |
| R4 (P1) | Claim-guard rejections leave ledger entries `published` forever | `recover` gains failed/ reconciliation: scans `failed/*.error.json`, transitions matching non-terminal ledger entries to `failed` with the reason; control rejects (no task_id/ledger entry) are skipped. |
| R5 (P2) | 0.5.0 tasks (write=[]) with out-of-cwd artifacts would hard-fail complete mid-rollout | Bounds enforcement gated on `permissions.write` being non-empty (the 0.6 send default declares it): legacy tasks get string passthrough + `artifact_warnings` instead of failure. Existence stays hard-enforced for all. |
| R6 (P2) | path_within Windows footguns (case, cross-drive ValueError) | Helper resolves both sides, compares via `os.path.normcase`, treats ValueError/cross-drive as not-contained. |
| R7 (P2) | Unbounded re-hash is an OA DoS vector | Verify order: is_file → stat vs recorded size_bytes → hash only on match. |
| R8 (P2) | Directory artifacts crash hashing; stat-then-stream cap TOCTOU; planted non-positive max_artifact_bytes | is_file() required; the cap is enforced while streaming; non-positive/absent cap treated as unset at complete. |
| R9 (P2) | test_state_routing pins validate's emit shape | Emit keys and verdict enum unchanged; decision block is additive and persisted only in the ledger. |
| R10 (P2) | Permission validation must keep keys optional and must not deny ancestors of control subtrees | Type-check keys only when present; deny only paths at-or-under `mailbox/ var/ control/` — the bus root itself (their ancestor) stays legal, so legacy `read=[root]` publishes fine. Claim does not re-validate permission entries. |
| R11 (P3) | Claim-side cwd-exists check breaks lazy workspaces and feeds R4 | Claim guard enforces the deny-set only; existence is send's job. |

Stated limitation (documented, not solved): sha256 provenance detects post-submit mutation only — it certifies the bytes the LWAR chose to submit, it does not make them trustworthy.

## Risks

- Existing tests publish with `cwd=root` (bus root itself) — allowed by D1 on purpose; only control-surface subtrees are denied.
- `complete` rewrites artifact list — result normalization already rebuilds the payload, so no schema_version bump needed; enum/object widening is additive.
- Windows symlink creation needs privileges — the symlink test self-skips when `os.symlink` raises.
