# Failure Taxonomy

Node-kind × failure-mode catalog for Phase 2 expansion. Each entry: detection signal + PG recovery pattern. Use as checklist — not every mode applies to every node, but explicitly mark non-applicable ones as N/A with a reason (silence = missed).

## Contents

- [1. Deterministic `[D]` Nodes](#1-deterministic-d-nodes)
- [2. Non-Deterministic `[AI]` Nodes](#2-non-deterministic-ai-nodes)
- [3. External `[EXT]` Dependencies](#3-external-ext-dependencies)
- [4. Structural / Pipeline-Level](#4-structural--pipeline-level)
- [5. Multi-Agent (delegate / [parallel] with agents)](#5-multi-agent-delegate--parallel-with-agents)
- [6. Operational / Organizational](#6-operational--organizational)

## 1. Deterministic `[D]` Nodes

| Failure mode | Detection signal | PG recovery pattern |
|---|---|---|
| Input validation bypass | unvalidated field reaches sink; 500 on malformed input | validate at node entry; reject with typed error |
| Boundary/overflow | off-by-one, int overflow, date rollover | property-based tests on boundaries; clamp + assert |
| Timeout | p99 latency SLO breach; hung connection | deadline per call; `try/except TimeoutError` + degrade |
| Resource exhaustion | OOM kill, disk full, connection pool drain | bounds on allocation; backpressure; quota checks |
| Concurrency/race | intermittent corruption under load; lost update | idempotent writes; optimistic locking; serialize critical section |
| Partial write | record exists in DB A, missing in DB B | transaction/outbox pattern; compensating action |
| Idempotency violation | duplicate charge/record after retry | idempotency key; dedup store; exactly-once semantics |
| Data corruption | checksum mismatch; impossible field values | schema validation on read; quarantine + alert |
| Silent bad result | no error, but wrong output downstream | `acceptance_criteria` on output invariants |

## 2. Non-Deterministic `[AI]` Nodes

These failures do not throw exceptions — outputs remain syntactically valid while being wrong. Detection always requires verification, not error handling.

| Failure mode | Detection signal | PG recovery pattern |
|---|---|---|
| Hallucination | fabricated citations/IDs/values; output unverifiable against source | `AI_verify(result, acceptance_criteria)` against ground truth; citation-required criteria |
| Format/schema drift | downstream parser breaks; JSON invalid or fields missing | schema-constrained generation; validate + reask with error feedback |
| Refusal / policy block | "I can't help with that"; unexpected fallback model in harness | detect refusal marker; route to fallback node; log policy collision |
| Context overflow | truncated input; forgotten early instructions; rising token count per call | chunk + map-reduce; summarize state; hard token budget per `AI_` call |
| Reasoning-history loss | harness fails to pass back thinking content; quality collapse after model switch mid-session | pin harness with verified compatibility; never switch model mid-session; checkpoint state |
| Over-agency | unrequested decisions; scope creep beyond instructions | explicit behavioral bounds in system prompt / AGENTS.md; `acceptance_criteria` limiting action scope |
| Under-specification drift | same input, divergent outputs across runs | tighten PPR spec; reduce `AI_` freedom for fragile steps; seed/temperature pinning where supported |
| Convergence non-termination | loop score oscillates below threshold; cost grows | max_iterations cap in POLICY; escalate with partial draft + failure report |
| Model-version regression | previously passing criteria fail after provider upgrade | acceptance_criteria suite as regression tests; version pinning; A/B before cutover |
| Evaluator bias | AI judge inflates scores of its own outputs | independent judge model; rubric-anchored scoring; human spot-check |

## 3. External `[EXT]` Dependencies

| Failure mode | Detection signal | PG recovery pattern |
|---|---|---|
| Unavailability / outage | 5xx, connection refused; status page incident | circuit breaker + cached/degraded fallback |
| Rate limit / quota | 429 responses; quota dashboard near limit | token bucket; backoff with jitter; queue and drain |
| Latency spike | p99 climb without errors | hedged requests; timeout + alternate provider |
| Contract change | new/removed fields; version deprecation notice | contract tests; schema tolerance (ignore unknown); provider version pinning |
| Auth expiry | 401/403 bursts | credential rotation monitor; refresh-before-expiry |
| Supply-chain / service discontinuation | provider sunset notice; export-control or region blocks | second-source abstraction layer; documented substitution plan |
| Cost explosion | bill anomaly; per-call price change | budget circuit breaker; per-node cost caps; route to cheaper tier |

## 4. Structural / Pipeline-Level

Derived from PG semantics — check every system modeled in PG against these.

| Failure mode | PG source | Analysis focus |
|---|---|---|
| Pipeline halt + stale output | `->` stage failure returns last successful output | Who consumes the stale value? Is staleness visible? |
| Missing error boundary | stage without `try/except` kills entire pipeline | Every stage that can fail independently needs its own boundary |
| `[parallel]` partial failure | block completes with subset of branches | Define min_success; define merge semantics with missing branches |
| Straggler branch | slowest parallel branch gates the join | Per-branch deadline; speculative cancellation |
| Merge conflict | two branches write shared resource | Declare shared resources in Phase 1; require ownership or locking |
| Circular dependency / fallback loop | A's fallback invokes B, B's fallback invokes A | Topological check on `@dep:` + fallback edges |
| Retry storm | N nodes retry a downed `[EXT]` simultaneously | Coordinated backoff; single retry owner; jitter |
| Fan-in blast radius | one node feeding many dependents | SPOF candidates; prioritize redundancy here |
| Depth overflow | tree beyond 5 levels hides failures deep in `(decomposed)` | Analyze decomposed subtrees as separate models |

## 5. Multi-Agent (delegate / [parallel] with agents)

Failure modes documented across agent frameworks (CrewAI, AutoGen, LangGraph-style systems); apply when Phase 1 finds agent delegation.

| Failure mode | Detection signal | PG recovery pattern |
|---|---|---|
| Infinite delegation loop | A delegates to B, B re-delegates to A; no terminal output | delegation chain depth ≤ 3; explicit stop condition in TaskSpec |
| Non-deterministic crew output | same task, different result each run — untestable | acceptance_criteria per agent; deterministic verifier at integration |
| Token/cost explosion | layered managers each re-prompt; hierarchical mode ~30%+ overhead vs sequential | budget caps per agent; flatten hierarchy; remove redundant manager layers |
| Missing termination condition | conversation rounds grow until context limit | max_rounds in TaskSpec; termination criteria explicit |
| Context window overflow in handoff | delegated task loses parent context | pass minimal sufficient state, not full history; summarize at handoff |
| Agent error boundary absent | one agent's exception kills the swarm | try/except per agent task; partial result integration policy |
| Authority overreach | agent modifies outside its scope | AuthorityBounds (can_create/can_modify/forbidden) in TaskSpec |

## 6. Operational / Organizational

| Failure mode | Detection signal | Notes |
|---|---|---|
| Observability gap | incident found by user before dashboard | No detection signal = highest-priority fix |
| Data retention/policy conflict | provider retains payloads; compliance flags | Map data classes to provider policies |
| On-call / ownership void | alerts without owner | Every Critical finding needs a named owner in report |
| Recovery untested | runbook exists but never executed | Recommend game-day per Critical recovery policy |
| Knowledge silo | only one person understands node | Flag as resilience risk alongside technical ones |
