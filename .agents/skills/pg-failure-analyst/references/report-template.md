# Report Template

Structure for `failure-analysis-{SystemName}.md`. Sections 1–7 are mandatory; omit a subsection only with an explicit N/A reason. All tables: no separate "Source" column — put evidence inline in cells.

---

```markdown
# {SystemName} Failure Analysis Report

**Analysis date:** YYYY-MM-DD · **Target:** {codebase path / doc / description} · **Scope:** {whole system | module}

## 0. Executive Summary

{3–5 sentences: system shape, headline risks, coverage stats. State Critical/High counts up front.}

## 1. System PG Model

```{lang}
{SystemName} // {description} (analyzed)
    {NodeA} [D] // ... @dep:...
    {NodeB} [AI] // ...
```

- Atomic nodes: {N} · Kinds: [D] {n} / [AI] {n} / [EXT] {n}
- `[parallel]` regions: {n} · Shared resources: {list} · `#incident-history`: {nodes}

## 2. Failure Mode Inventory

| Node | Failure mode | Kind | Detection signal | Evidence |
|---|---|---|---|---|
| {Node} | {mode} | [D]/[AI]/[EXT] | {signal} | {file:line / doc § / log id} |

## 3. Error Propagation Paths

| Path | Origin failure | Propagation chain | Affected nodes | Notes |
|---|---|---|---|---|
| P1 | {node: mode} | A -> B -> C | {n} | stale output consumed by: {node} |

- **SPOFs:** {list} · **Fan-in points:** {list} · **Fallback loops:** {list or none}
- `[parallel]` partial-failure combinations: {block name}: risky {list} among {n} combinations

## 4. Edge Case Catalog

| Generator | Case | Affected node | Expected behavior | Actual/likely behavior | Propagates? |
|---|---|---|---|---|---|
| Boundary | {e.g. amount=0} | {node} | {expected} | {actual} | {Y: path / N} |

(Cover all six generators — boundary, scale, timing, state, input, and AI input — with at least one row each or an explicit N/A reason.)

## 5. Priority Matrix

| ID | Finding | Severity | Blast radius | Likelihood | Score | Grade |
|---|---|---|---|---|---|---|
| F-01 | {finding} | 3 | 3 | 2 | 18 | Critical |

Grade thresholds: Critical ≥18 · High ≥12 · Medium ≥6 · Low <6

## 6. Recovery Policies (PG)

### F-01 {finding name}

```python
{PPR snippet: try/except | Failure Strategy | partial_ok | circuit_breaker}
# acceptance_criteria: {verifiable condition}
```

(Required for every Critical/High finding. Medium and Low findings may be summarized in a table.)

## 7. Verification Results

- [ ] Consistency: every propagation path references an actual `@dep:` or `->` edge
- [ ] Completeness: atomic nodes with failure modes {n}/{n} · `[parallel]` regions analyzed {n}/{n} · Critical/High recovery policies {n}/{n}
- [ ] Correctness: every finding cites evidence and follows the scoring rule

## Appendix: Coverage Statistics

- Analyzed nodes: {n} · Average failure modes per node: {x} · Explicit N/A entries: {n}
- Uncovered areas: {list + reason}
```

---

## Authoring Notes

- Numbers in §0 must match §5 counts exactly.
- Every §2 row needs evidence; rows without evidence move to the Appendix under "Uncovered areas".
- §6 snippets must be valid PPR (Python-like, `AI_` only for cognition, actual code for deterministic checks).
- Keep tables scannable: one finding per row, no multi-line cells.
