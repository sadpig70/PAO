# WORKPLAN-{Name}.md + Loop Skill Execution Engine

## 1. PGF as Executable Work Plan

PGF functions beyond a simple design tool as an **executable work plan**.

### Advantages Over Traditional Methods

| Checklist / Jira | PGF Work Plan |
|---|---|
| Linear task order | BFS hierarchy + `[parallel]` execution |
| Failure = manual intervention | Failure Strategy defined in PPR |
| Status: checked/unchecked | Core execution state transition (`designing → in-progress → done/blocked`) |
| Dependencies: manual tracking | `@dep:` parsed for automatic ordering |
| Design changes: external approval | `AI_redesign` authority inline in PPR |
| Completion: subjective | `acceptance_criteria` embedded in PPR — verifiable |

### POLICY Block

Declare global execution policies at the top of WORKPLAN-{Name}.md:

```python
POLICY = {
    "_version":            "2.5",  # Optional. Schema version. Absent = current PGF version.
    "max_retry":           3,
    "on_blocked":          "skip_and_continue",
    "design_modify_scope": ["impl", "internal_interface"],
    "completion":          "all_done_or_blocked",
    "max_iterations":      50,
}
```

| Field | Description | Default |
|---|---|---|
| `max_retry` | Maximum retry count per node execution | `3` |
| `on_blocked` | Strategy on blocking (`skip_and_continue`, `halt`) | `"skip_and_continue"` |
| `design_modify_scope` | Scope AI is allowed to redesign | `["impl", "internal_interface"]` |
| `completion` | Termination condition (`all_done_or_blocked`, `all_done`) | `"all_done_or_blocked"` |
| `max_verify_cycles` | Maximum verify → rework iteration count (full-cycle/create mode) | `2` |
| `verify_perspectives` | List of verification perspectives | `["performance", "security", "maintainability"]` |
| `discovery_personas` | Number of personas used in discovery phase (create mode) | `8` |
| `auto_select` | Enable automatic idea selection (create mode) | `true` |
| `min_vote_threshold` | Minimum vote count for auto-selection (0 = no limit) | `0` |
| `max_iterations` | Maximum loop iteration count | `50` |
| `_version` | POLICY schema version (optional, absent = current PGF version) | `"2.5"` |
| `delegation_max_depth` | Maximum delegation chain depth | `3` |

### WORKPLAN-{Name}.md Example

```
POLICY = {
    "max_retry":           3,
    "on_blocked":          "skip_and_continue",
    "design_modify_scope": ["impl", "internal_interface"],
    "completion":          "all_done_or_blocked",
    "max_iterations":      50,
}

ProductLaunch // Product launch execution (in-progress) @v:1.0

    Research // Market & technical research (done)
        [parallel]
        AI_analyze_market_opportunity  // Market analysis (done)
        AI_assess_technical_feasibility // Tech assessment (done)
        AI_identify_competitive_gaps    // Competitive intelligence (done)
        [/parallel]

    Design // System design (in-progress) @dep:Research
        AI_generate_architecture       // Architecture generation (in-progress)
        AI_design_user_experience      // UX design (designing)

    Implementation // Build (designing) @dep:Design
        CoreSystem   // Core implementation (designing)
        TestSuite    // Test suite (designing)

    Launch // Launch execution (blocked) @dep:Implementation
        AI_generate_launch_materials   // Launch content (blocked)
        DeploySystem                    // Deployment (blocked)
```

---

## 2. DESIGN → WORKPLAN Conversion

### Conversion Procedure

```python
def convert_design_to_workplan(design_path: str, workplan_path: str, policy: dict):
    """Convert DESIGN-{Name}.md Gantree to WORKPLAN-{Name}.md"""
    design = Read(design_path)
    gantree = AI_extract_gantree_section(design)

    # 1. Initialize all node statuses to (designing)
    workplan_tree = reset_all_status(gantree, target="designing")

    # 2. Set top-level nodes without dependencies to (in-progress)
    first_nodes = [n for n in workplan_tree.top_level_nodes if not n.dependencies]
    for n in first_nodes:
        n.status = "in-progress"

    # 3. Output WORKPLAN-{Name}.md
    Write(workplan_path, format_workplan(policy, workplan_tree))

    # 4. Initialize status-{Name}.json
    init_status_json(workplan_path)
```

### Conversion Notes

- PPR def blocks are not copied into the WORKPLAN — referenced from DESIGN-{Name}.md
- Nodes marked `(decompose)` are kept as-is — referenced from separate .md files
- Nodes marked `(blocked)` in DESIGN remain `(blocked)` in WORKPLAN

### Lightweight WORKPLAN (Standalone Without DESIGN-{Name}.md)

WORKPLAN-{Name}.md can be used standalone without DESIGN-{Name}.md. In this case, task specifications are written directly as `#` inline comments under nodes.

```
TaskNode // Task description (designing)
    # Task: what to perform
    # Target: file path or module
    # Output: result file
    # criteria: completion criteria
```

**Suitable for**: Simple tasks, document writing, refactoring, configuration changes — cases where PPR def blocks are excessive.

**`#` Comment Rules**:
- Placed at deeper indentation than the node line
- Free-form — sufficient as long as AI can understand the intent
- Comments end when the next node line (same indentation `NodeName //`) appears
- The Loop engine's `extract-ppr.py` automatically extracts and injects into the prompt

---

### POLICY Templates

```python
# Standard development project
POLICY_STANDARD = {
    "max_retry":           3,
    "on_blocked":          "skip_and_continue",
    "design_modify_scope": ["impl", "internal_interface"],
    "completion":          "all_done_or_blocked",
    "max_iterations":      50,
}

# Strict sequential execution (finance, healthcare, etc.)
POLICY_STRICT = {
    "max_retry":           5,
    "on_blocked":          "halt",
    "design_modify_scope": ["impl"],
    "completion":          "all_done",
    "max_iterations":      50,
}

# Exploratory / prototype project
POLICY_EXPLORATORY = {
    "max_retry":           2,
    "on_blocked":          "skip_and_continue",
    "design_modify_scope": ["impl", "internal_interface", "public_interface"],
    "completion":          "all_done_or_blocked",
    "max_iterations":      50,
}
```

---

## 3. Node State Transition Rules

### State Transition Diagram

```
designing ──→ in-progress ──→ done
                  ↘ blocked (blocker occurred)
                       ↘ in-progress (manual, when blocker resolved)
```

### Automatic Transition Conditions

| Transition | Trigger | AI Action |
| --- | --- | --- |
| `designing → in-progress` | Selected by `select_next_node()` | Verify all `@dep:` are done, then start execution |
| `in-progress → done` | `AI_verify_result()` passed | Update WORKPLAN-{Name}.md + status-{Name}.json |
| `in-progress → blocked` | `max_retry` exhausted | Document blocker → apply `on_blocked` policy |

### [parallel] Block Partial Failure Handling

When some nodes within a `[parallel]` block fail (become blocked):

| Situation | Handling |
|------|------|
| All succeed | Normal completion, proceed to next step |
| Partial failure + `on_blocked: skip_and_continue` | Successful nodes marked `(done)`, failed nodes marked `(blocked)`, proceed to next step |
| Partial failure + `on_blocked: halt` | Entire block halted, report to user |
| All fail | Apply `on_blocked` policy (skip → entire block blocked, halt → stop) |

If successor nodes of a `[parallel]` block depend via `@dep:` on a failed node, those successor nodes are also marked `(blocked)`.

### Update Protocol

When node status changes, **always** update two locations simultaneously:

1. Modify the `(status)` text of the corresponding node in the `WORKPLAN-{Name}.md` file
2. Update node status + timestamp + summary in `status-{Name}.json`

---

## 4. Loop Skill — Self-Executing PGF

Loop Skill is the most complete expression of PGF's parser-free property: **the execution engine itself is specified in PGF**. Claude reads loop-reference.md to understand the execution algorithm, then applies it to WORKPLAN-{Name}.md.

### Architecture

```
DESIGN-{Name}.md        →  System architecture
     ↓ generates
WORKPLAN-{Name}.md      →  Executable work plan (includes AI cognitive nodes)
     ↓ interpreted by
loop-reference.md    →  Execution engine (itself a PGF document)
     ↓ runtime
Claude            →  AI runtime (runtime, not a tool)
```

### Core Algorithm

```python
def select_next_node(workplan: WorkPlan) -> Optional[GantreeNode]:
    """Select next execution node — respects @dep: and [parallel]"""
    candidates = [
        n for n in workplan.nodes
        if n.status in ["in-progress", "designing"]
        and all(d.status == 'done' for d in n.dependencies)
    ]
    return AI_prioritize_node(candidates, workplan.policy) if candidates else None

def run_loop(workplan_path: str) -> LoopResult:
    """Main loop — executes until all_nodes_terminal()"""
    workplan  = parse_pgf(workplan_path)
    iteration = 0
    while iteration < workplan.policy.max_iterations:
        node = select_next_node(workplan)
        if node is None:
            return LoopResult(status='COMPLETE')
        result = resilient_execution(node, workplan.policy)  # Apply Failure Strategy
        update_workplan_state(workplan_path, node, result)
        if all_nodes_terminal(workplan):
            return LoopResult(status='COMPLETE')
        iteration += 1
    return LoopResult(status='MAX_ITERATIONS_REACHED')
```

### DESIGN Synchronization on Redesign

When `AI_redesign` modifies a node's internal implementation during Failure Strategy execution, the corresponding PPR def block in DESIGN-{Name}.md must be updated to prevent DESIGN ↔ implementation drift. The executor should update the DESIGN file's PPR section for the affected node after a successful redesign.

### Invocation

```bash
# For internal algorithm explanation — actual CLI invocation uses `/PGF loop start`
/PGF loop start \
    --workplan ./WORKPLAN-{Name}.md \
    --skill    ./loop-reference.md \
    --max-iterations 50
```

Steps performed by Claude:
1. Read loop-reference.md → understand execution algorithm
2. Read WORKPLAN-{Name}.md → understand task tree and AI nodes
3. Execute nodes respecting `@dep:` order + `[parallel]` blocks
4. On failure → apply PPR-defined Failure Strategy, AI redesign within scope
5. Update WORKPLAN-{Name}.md status after each node execution
6. Terminate when `all_nodes_terminal() == true`

---

## 5. Error Recovery Scenarios

### Resuming After Session Interruption

```python
def resume_from_interruption(workplan_path: str, status_path: str):
    """Resume from WORKPLAN state after session interruption"""
    status = json.loads(Read(status_path))
    workplan = Read(workplan_path)

    # 1. Check nodes that were in-progress → re-execute from those nodes
    in-progress = [n for n, s in status["nodes"].items() if s["status"] == "in-progress"]

    # 2. Verify consistency between status-{Name}.json and WORKPLAN-{Name}.md
    for node_name, node_data in status["nodes"].items():
        workplan_status = extract_status_from_workplan(workplan, node_name)
        if workplan_status != node_data["status"]:
            # On mismatch, correct status-{Name}.json based on WORKPLAN-{Name}.md
            node_data["status"] = workplan_status

    Write(status_path, json.dumps(status, indent=2))
    return run_loop(workplan_path)  # Continue from interruption point
```

### When status-{Name}.json Is Corrupted

1. Parse node statuses from WORKPLAN-{Name}.md to regenerate status-{Name}.json
2. Timestamps cannot be recovered — initialize with current time
3. Recalculate summary

### Resuming After Manual WORKPLAN-{Name}.md Editing

When a user manually edits WORKPLAN-{Name}.md:
1. Detect differences between status-{Name}.json and WORKPLAN-{Name}.md node statuses
2. Treat WORKPLAN-{Name}.md as the source of truth
3. Synchronize status-{Name}.json based on WORKPLAN-{Name}.md
4. Resume `run_loop()`

### Resuming via /reopen-session

1. `/reopen-session` → load WORKPLAN-{Name}.md path from handoff document
2. Load status-{Name}.json → check last progress state
3. Resume execution from the interrupted node
