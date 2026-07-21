---
name: pg-failure-analyst
description: "PG/PGF-based failure analysis for existing systems. Reverse-models a system (codebase, design doc, architecture description, or logs) into PG notation — Gantree structure, @dep: dependencies, arrow pipelines, [parallel] blocks, AI_ nodes — then systematically derives failure modes, error propagation paths, edge cases, and incident risks, and outputs a prioritized Markdown report with PG-annotated recovery policies. Auto-load on: FMEA, edge case derivation, failure mode analysis, error propagation, fault analysis, PG-based error analysis, failure analysis report, and system risk discovery. Depends on pg and pgf skills."
---

# PG Failure Analyst

Derive errors, edge cases, and failure modes from an existing system by first modeling the system in PG notation, then expanding failure knowledge over that model. Output is a Markdown report.

**PG** supplies the system model (Gantree + PPR). **PGF** supplies the analysis discipline (status tracking, POLICY, 3-perspective verification). This skill supplies the failure-methodology (taxonomy, propagation tracing, prioritization) that neither has built in.

## When Inputs Are Missing

Require at least one analysis target: codebase path, design/architecture document, system description, or log/incident history. If none is given, ask once for the target and its scope (whole system vs. named module). Do not invent a system's structure.

## Analysis Pipeline

Execute six phases in order. For systems with >30 nodes, track the analysis itself as a PGF WORKPLAN with status codes; otherwise run inline.

```
FailureAnalysis // analyze system failures (in-progress) @v:1.0
    Phase1_MapStructure      // target system -> PG structure model (designing)
    Phase2_FailureModes      // expand node x failure modes (designing) @dep:Phase1_MapStructure
    Phase3_Propagation       // trace error propagation paths (designing) @dep:Phase2_FailureModes
    Phase4_EdgeCases         // derive boundary and exceptional cases (designing) @dep:Phase3_Propagation
    Phase5_PrioritizeRecover // prioritize + define PG recovery policies (designing) @dep:Phase4_EdgeCases
    Phase6_Report            // produce the Markdown report (designing) @dep:Phase5_PrioritizeRecover
```

### Phase 1 — Structure Mapping

Reverse-engineer the target into a Gantree (same goal as `/PGF design --analyze`). Rules:

- Decompose to atomic nodes (15-minute rule); mark over-large nodes `(decomposed)` and analyze them as sub-models.
- Annotate every node with its execution kind — this drives which failure taxonomy applies in Phase 2:
  - `[D]` deterministic: exact computation, I/O, DB, network call
  - `[AI]` non-deterministic: any `AI_` cognition (Judgment/Reasoning/Recognition/Creation)
  - `[EXT]` external dependency: third-party API, SaaS, human-in-the-loop
- Capture `@dep:` chains, `->` pipeline stage order, `[parallel]` regions, and shared resources (DB, queue, file, cache).
- If logs/incident history are provided, mark historically failed nodes with `#incident-history`.

### Phase 2 — Failure Mode Expansion

For every atomic node, expand failure modes from the taxonomy in [references/failure-taxonomy.md](references/failure-taxonomy.md). Minimum coverage per node kind:

- `[D]` nodes: input validation, boundary/overflow, timeout, resource exhaustion, concurrency, partial write, idempotency violation.
- `[AI]` nodes: hallucination, format/schema drift, refusal, context overflow, reasoning-history loss, over-agency, convergence non-termination, model-version regression.
- `[EXT]` nodes: unavailability, rate limit/quota, latency spike, contract change, auth expiry.

Record each mode with its **detection signal** (how the system would notice) and **evidence** (code line, doc section, or log entry — never assert a failure mode without pointing at what suggests it).

### Phase 3 — Propagation Tracing

Trace how each Phase-2 failure travels:

- Follow `@dep:` and `->` chains downstream: upstream failure → which nodes block, degrade, or receive stale/last-successful output (PG pipeline semantics: stage failure halts the pipeline and returns the last successful output — flag every place that stale output is consumed as if fresh).
- For each `[parallel]` block, enumerate partial-failure combinations (1-of-N failed, slowest-branch straggler, merge with missing branch).
- Identify SPOFs (single points of failure), fan-in convergence points, and compute blast radius = count of transitively affected nodes.
- Check for circular failure loops: node A's fallback calls node B whose fallback calls A; retry storms amplifying an `[EXT]` outage.

### Phase 4 — Edge Case Derivation

Derive edge cases from six generators; apply each generator to every node where applicable:

1. **Boundary values**: 0, negative, empty, null, max/max+1, unit mismatch (currency, timezone, encoding).
2. **Scale**: N=1, N=10⁶, payload at context/size limit, payload 1 byte over limit.
3. **Timing**: timeout mid-write, retry during partial completion, clock skew, ordering inversion in `[parallel]`.
4. **State**: empty DB, corrupt record, concurrent modification, schema version skew.
5. **Input**: malformed, adversarial/injection, multilingual, duplicated (idempotency key reuse).
6. **AI-input** (for `[AI]` nodes): ambiguous instruction, conflicting constraints, out-of-distribution input, mid-session model switch, harness failing to preserve thinking history.

For each edge case state: affected node, expected behavior, actual/likely behavior, and whether Phase 3 shows it propagating.

### Phase 5 — Prioritization & Recovery Policy

Score every finding: **Priority = Severity × Blast radius × Likelihood** (each 1–3). Map to Critical(≥18) / High(≥12) / Medium(≥6) / Low. Then attach a PG recovery policy to every Critical/High finding using these patterns:

```python
# Pattern 1: isolate a pipeline failure and continue with marked stale output
try:
    enriched = raw -> AI_extract -> AI_classify
except PipelineStageError as e:
    enriched = e.last_successful_output  # freshness marker is mandatory
    flag_stale(enriched, failed_stage=e.stage)

# Pattern 2: validate and redesign an AI_ node (PGF Failure Strategy)
for attempt in range(max_retry := 3):
    result = AI_execute(task)
    if AI_verify(result, acceptance_criteria):
        return result
    if attempt >= 1:
        task.ppr = AI_redesign(task, result.failure_reason,
                               constraint='preserve_public_interface')
task.status = "blocked"  # escalate

# Pattern 3: handle partial failure in [parallel]
[parallel]
results = await_all(tasks, policy="partial_ok", min_success=2)
[/parallel]
if results.failures:
    results = compensate(results.failures, strategy="retry_once_then_degrade")

# Pattern 4: isolate an EXT dependency
with circuit_breaker(ext_api, threshold=5, cooldown=60):
    data = ext_api.fetch()
# while open: cache_fallback() or graceful_degrade()
```

Every recovery policy must embed `acceptance_criteria` so model upgrades can regression-test the fix (co-evolution property).

### Phase 6 — Report

Write the Markdown report per [references/report-template.md](references/report-template.md). Save as `failure-analysis-{SystemName}.md` in the user's output location; when working inside a PGF project, also copy to `.pgf/FAILURE-ANALYSIS-{Name}.md`.

## Verification (run before delivering)

Apply PGF's 3-perspective check to the analysis itself:

- **Consistency**: no finding contradicts the Phase-1 model; every propagation path references real `@dep:`/`->` edges.
- **Completeness**: every atomic node has ≥1 failure mode; every `[parallel]` block has partial-failure analysis; every Critical/High finding has a recovery policy with `acceptance_criteria`.
- **Correctness**: every claimed failure mode cites evidence (code/doc/log); severities match the scoring rule.

Coverage gate: report is blocked if any atomic node lacks a failure mode or any Critical finding lacks a recovery policy.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Failure modes without evidence | Phase 2 requires a pointer to code/doc/log per mode |
| Only deterministic failures listed | `[AI]` nodes have their own taxonomy — apply it |
| Ignoring stale-output semantics | PG pipelines return last successful output on failure — trace who consumes it |
| [parallel] treated as all-or-nothing | Enumerate partial-failure combinations explicitly |
| Recovery without acceptance_criteria | Unverifiable fixes cannot regression-test on model upgrade |
| Analysis drift from actual system | Re-anchor Phase 1 model when Phase 2–4 finds undocumented behavior; note the discrepancy in the report |
