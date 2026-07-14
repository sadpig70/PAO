# Cycle Modes — full-cycle / create Continuous Auto-Execution Specification

> **Unified reference for the two cycle modes.** Both share the **DESIGN → PLAN → EXECUTE → VERIFY** backbone; `create` prepends a **DISCOVER** phase that ends with automatic idea selection.

## 1. Overview

| Mode | Starts From | Phases | Idea Source | Halts For User? |
|---|---|---|---|---|
| `full-cycle` | User-provided description | 4 (DESIGN→VERIFY) | Given by user | No (error only) |
| `create` | IdeaFirst discovery engine | 5 (DISCOVER→VERIFY) | `auto_select_idea` vote | No (error or 0 votes only) |

**Relationship**: `create` = DISCOVER phase + `full-cycle`. The 4-phase core (DESIGN/PLAN/EXECUTE/VERIFY + rework loop + session resume) is identical between the two modes. All mode-specific logic is isolated to Phase 1 (DISCOVER) and the `--skip-discover` option.

---

## 2. Commands

| Command | Action |
|---|---|
| `/PGF full-cycle {name}` | 4-phase continuous execution from user description |
| `/PGF full-cycle {name} --max-verify-cycles N` | Up to N verify-rework iterations (default 2) |
| `/PGF full-cycle {name} --with-review[=N]` | Insert opt-in design review gate after DESIGN (default N=1) |
| `/PGF create` | 5-phase autonomous execution (DISCOVER + 4-phase core) |
| `/PGF create --skip-discover` | Start from DESIGN using existing `final_idea.md` |
| `/PGF create --personas N` | Use N personas in DISCOVER phase (default 8) |

---

## 3. Unified Execution Sequence

```python
def cycle(
    mode: Literal["full-cycle", "create"],
    project_root: str,
    description: str | None = None,      # full-cycle: user description
    policy: CyclePolicy = None,
    with_review: int = 0,                # full-cycle opt-in
    skip_discover: bool = False,         # create only
    personas_n: int = 8,                 # create only
) -> CycleResult:
    """Unified design→verify pipeline. create mode prepends DISCOVER as Phase 1."""
    pgf_dir = f"{project_root}/.PGF"
    policy = policy or POLICY_STANDARD

    # ═══ Phase 1: DISCOVER (create mode only) ═══
    if mode == "create":
        if skip_discover:
            final_results = parse_final_idea_for_skip(pgf_dir)
        else:
            discovery_dir = f"{pgf_dir}/discovery"
            personas   = load_personas(n=personas_n)
            news       = step_news_collect(personas, discovery_dir)
            trends     = step_trend_analysis(personas, news, discovery_dir)
            insights   = step_insight_extract(personas, trends, discovery_dir)
            ideas      = step_idea_generation(personas, insights, discovery_dir)
            candidates = step_top_selection(personas, ideas, discovery_dir)
            final_results = step_final_selection(personas, candidates, discovery_dir)
            archive_discovery(discovery_dir)   # → archive-discovery.py invocation
        seed = auto_select_idea(final_results)
        report_phase("DISCOVER", seed)
    else:
        seed = description

    # ═══ Phase 2: DESIGN ═══
    design_path = f"{pgf_dir}/DESIGN-{seed.name}.md"
    AI_design_gantree(seed, design_path)
    assert AI_validate_design(design_path), "design incomplete"
    report_phase("DESIGN", design_path)

    # ─── Phase 2.5: DESIGN REVIEW (full-cycle opt-in, --with-review[=N]) ───
    if mode == "full-cycle" and with_review > 0 and not is_level_1(design_path):
        review_result = run_design_review_loop(
            design_path,
            max_iterations=with_review,
            status_path=f"{pgf_dir}/status-{seed.name}.json",
        )
        # status JSON에 review_iterations / unresolved_issues 만 추가
        if review_result.status == "needs_user_ack":
            # 강제 차단 아님 — 잔여 이슈 보고 후 사용자 승인 시 plan 진행
            if not AI_request_user_ack(review_result.unresolved_issues):
                return CycleResult(status="blocked",
                                   issues=review_result.unresolved_issues)
        report_phase("DESIGN_REVIEW",
                     f"iterations: {review_result.iterations}, "
                     f"unresolved: {len(review_result.unresolved_issues)}")

    # ═══ Phase 3: PLAN ═══
    workplan_path = f"{pgf_dir}/WORKPLAN-{seed.name}.md"
    status_path   = f"{pgf_dir}/status-{seed.name}.json"
    convert_design_to_workplan(design_path, workplan_path, policy)
    init_status_json(workplan_path, status_path)
    report_phase("PLAN", workplan_path)

    # ═══ Phase 4: EXECUTE ═══
    execute_all_nodes(workplan_path, design_path, status_path)
    report_phase("EXECUTE", "all nodes terminal")

    # ═══ Phase 5: VERIFY (with rework loop) ═══
    for cycle in range(policy.get("max_verify_cycles", 2)):
        verify_result = verify_project(design_path, workplan_path, policy)
        if verify_result.status == "passed":
            report_phase("VERIFY", "passed")
            return CycleResult(status="completed",
                               design=design_path, workplan=workplan_path)
        elif verify_result.status == "blocked":
            report_phase("VERIFY", "blocked")
            return CycleResult(status="blocked", issues=verify_result.issues)
        else:  # rework
            AI_rework_subtree(design_path, workplan_path, verify_result.issues)
            execute_all_nodes(workplan_path, design_path, status_path)

    report_phase("VERIFY", "rework_limit_exceeded")
    return CycleResult(status="rework_limit_exceeded")
```

---

## 4. Phase Transition Conditions

| Transition | Condition | On Failure | Applies To |
|---|---|---|---|
| (start) → discover | mode = create | — | create |
| discover → design | `auto_select_idea` succeeds (≥1 vote) | Halt + request manual selection | create |
| (start) → design | mode = full-cycle | — | full-cycle |
| design → review (opt-in) | `--with-review[=N]` set, not Level 1, not micro | Skip review gate | full-cycle |
| review → plan | Critical=0 AND High≤2 (또는 사용자 ack) | revise (≤N회) → DESIGN 재작성 후 재검토 | full-cycle |
| review → blocked | N회 초과 + 사용자 ack 거부 | report unresolved_issues | full-cycle |
| design → plan | 4 completion criteria met (atomized, PPR written, no @dep cycles, checklist passed) | Retry design (up to 3 times) | both |
| plan → execute | WORKPLAN + status JSON created | Error report + halt | both |
| execute → verify | All nodes terminal (`done` or `blocked`) | Continue execute | both |
| verify → complete | passed | rework → re-execute subtree / blocked → report | both |

> Design review gate detail: see `design-review-reference.md`. status JSON adds only `review_iterations` and `unresolved_issues` — existing keys remain unchanged.

---

## 5. Automatic Idea Selection — `auto_select_idea` (create mode only)

```python
def auto_select_idea(final_results: list[dict]) -> Idea:
    """Select optimal idea from STEP 6 results without user approval.

    1. Extract final selections from N personas
    2. Pick idea with most votes
    3. On tie → weighted score by novelty × impact
    4. Record rationale in creation_log.md
    """
    selections = AI_extract_selections(final_results)
    vote_counts = count_votes(selections)

    if not vote_counts or max(vote_counts.values()) == 0:
        log_selection(None, "FAILED_ZERO_VOTES", vote_counts, selections)
        raise ValueError(
            "auto_select_idea: 0 votes extracted. "
            "Manual selection required via /PGF discover."
        )

    if max(vote_counts.values()) >= 5:
        winner = max(vote_counts, key=vote_counts.get)
        consensus = "CONVERGED"
    else:
        top = [k for k, v in vote_counts.items() if v == max(vote_counts.values())]
        winner = max(top, key=lambda i: score_idea(i, weight="novelty*impact"))
        consensus = "DIVERGED_AUTO_SELECTED"

    log_selection(winner, consensus, vote_counts, selections)
    return winner
```

---

## 6. `--skip-discover` Option (create mode only)

When existing `final_idea.md` is available, skip the discovery phase and start from DESIGN:

```text
/PGF create --skip-discover
  → Load .pgf/discovery/final_idea.md
  → Apply auto_select_idea
  → Autonomous execution from Phase 2 (DESIGN)
```

```python
def parse_final_idea_for_skip(pgf_dir: str) -> list[dict]:
    """Convert final_idea.md to auto_select_idea input format."""
    path = f"{pgf_dir}/discovery/final_idea.md"

    if not exists(path):
        raise FileNotFoundError(
            f"final_idea.md not found at {path}. "
            "Run /PGF discover first, or provide the file manually."
        )

    content = Read(path)
    # final_idea.md is divided into ## [P{N}] sections per persona
    sections = AI_parse_persona_sections(content)

    if len(sections) == 0:
        raise ValueError("final_idea.md has no parseable persona sections.")

    return [
        {"persona": s.persona_id, "selection": AI_extract_selected_idea(s)}
        for s in sections
    ]
```

### Error on Missing File

```text
[ClNeo CREATE] ✗ --skip-discover failed
  final_idea.md not found: .pgf/discovery/final_idea.md
  → Run /PGF discover first, or provide the file manually.
```

---

## 7. Rework Regression Loop

```python
def AI_rework_subtree(design_path, workplan_path, issues):
    """Identify rework target nodes and roll back."""
    for issue in issues:
        node = issue.node
        AI_fix_design_ppr(design_path, node, issue)              # 1. fix PPR in DESIGN
        rollback_subtree(workplan_path, node, target_status="designing")  # 2. rollback node + children
    sync_status_json(workplan_path)                              # 3. sync status JSON
```

### Rollback Scope

```python
def identify_rework_scope(issues: list[VerifyIssue], workplan_path: str) -> list[str]:
    """Rework target node + all descendant nodes. Parent/siblings unaffected."""
    target_nodes = set(i.node for i in issues)
    descendants = set()
    for node in target_nodes:
        descendants.update(get_descendants(workplan_path, node))
    return list(target_nodes | descendants)
```

### Iteration Limit

- Allow verify → rework repetition up to `POLICY.max_verify_cycles` (default `2`)
- When exceeded:
  - Preserve all outputs produced so far (do not delete)
  - Halt with report including unresolved issues + attempt count
  - Return `CycleResult.status = "rework_limit_exceeded"`

---

## 8. Progress Report Format

### full-cycle

```text
[PGF FULL-CYCLE] Phase 1/4 DESIGN complete | nodes: 12 | DESIGN-{Name}.md
[PGF FULL-CYCLE] Phase 2/4 PLAN complete | WORKPLAN-{Name}.md + status-{Name}.json
[PGF FULL-CYCLE] Phase 3/4 EXECUTE complete | 12/12 nodes done
[PGF FULL-CYCLE] Phase 4/4 VERIFY complete | status: passed

[PGF FULL-CYCLE] === Complete ===
  Design: .pgf/DESIGN-{Name}.md
  Execution: .pgf/WORKPLAN-{Name}.md
  Verification: passed
```

### create

```text
[ClNeo CREATE] ✓ Phase 1/5 DISCOVER complete | idea: "{idea_name}" | consensus: CONVERGED
[ClNeo CREATE] ✓ Phase 2/5 DESIGN complete | nodes: 15 | DESIGN-{Name}.md
[ClNeo CREATE] ✓ Phase 3/5 PLAN complete | WORKPLAN-{Name}.md + status-{Name}.json
[ClNeo CREATE] ✓ Phase 4/5 EXECUTE complete | 15/15 nodes done
[ClNeo CREATE] ✓ Phase 5/5 VERIFY complete | status: passed

[ClNeo CREATE] ═══ Creation Complete ═══
  Idea: {idea_name}
  Design: .pgf/DESIGN-{Name}.md
  Implementation: {implementation_path}
  Verification: passed
```

### On Rework (both modes)

```text
[PGF ...] VERIFY | status: rework (cycle 1/2)
  Target nodes: TokenValidator, SessionManager
[PGF ...]   → rework: fixing design + re-executing...
[PGF ...] VERIFY complete | status: passed (cycle 2/2)
```

### On Rework Limit Exceeded

```text
[PGF ...] VERIFY | rework_limit_exceeded (2/2 cycles)
  Unresolved: TokenValidator (medium), SessionManager (medium)

[PGF ...] === Halted ===
  Design: .pgf/DESIGN-{Name}.md (preserved)
  Execution: .pgf/WORKPLAN-{Name}.md (preserved)
  Verification: rework_limit_exceeded
```

---

## 9. Session Interruption / Resume

WORKPLAN/status JSON are saved on each Phase completion → resumption is always possible.

| Phase Completion Point | Preserved Outputs |
|---|---|
| DISCOVER complete (create) | discovery/*.md + final_idea.md |
| DESIGN complete | DESIGN-{Name}.md |
| PLAN complete | DESIGN + WORKPLAN-{Name}.md + status-{Name}.json |
| EXECUTE complete | Above + execution outputs |
| VERIFY in progress | Above + partial verification results |

### Resume Procedure

1. Check last completed Phase from `status-{Name}.json`
2. Continue execution from the next phase
3. If interrupted during EXECUTE, resume from incomplete nodes

### `/reopen-session` Integration

Record cycle progress state in `PROJECT_STATUS.md`:

```text
## Cycle Progress State
- Mode: {full-cycle | create}
- Project: {project_name}
- Current Phase: EXECUTE (3/4 | 4/5)
- Completed nodes: 8/12
- Next action: Resume execution of incomplete nodes
```

---

## 10. Error Behavior

| Situation | Mode | Response |
|---|---|---|
| Discovery failure (majority of agents failed) | create | Halt + error report |
| 0-vote in `auto_select_idea` | create | Halt + request manual selection |
| Design validation failure (after 3 retries) | both | Preserve outputs + halt |
| WORKPLAN/status JSON creation failure | both | Error report + halt |
| Majority of nodes blocked during execution | both | Apply `POLICY.on_blocked` then continue |
| Verification rework limit exceeded | both | Preserve outputs + halt report |
| Verification blocked (high severity) | both | Preserve outputs + report to user + halt immediately |

---

## 11. Mode Comparison Recap

| Aspect | `/PGF full-cycle` | `/PGF create` |
|---|---|---|
| Phase count | 4 | 5 |
| Idea source | User description | IdeaFirst persona consensus |
| Idea selection | N/A | `auto_select_idea` (votes + novelty×impact tiebreak) |
| Design review gate | `--with-review[=N]` opt-in | Not currently exposed |
| `--skip-discover` | N/A | Bypass Phase 1, use `final_idea.md` |
| Breakpoints (user pause) | None | None |
| Typical entry | Already-formed task description | Open-ended exploration |
