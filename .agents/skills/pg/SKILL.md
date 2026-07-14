---
name: pg
description: "PG (PPR/Gantree) — AI-native intent specification notation. Gantree for hierarchical structure decomposition, PPR for detailed logic with AI_ cognitive functions, pipelines, and [parallel] blocks. Auto-load when encountering Gantree trees, PPR def blocks, AI_ prefixed functions, pipelines, or PG-written documents."
user-invocable: false
disable-model-invocation: false
---

# PG — PPR/Gantree Notation v1.4

> PG is a DSL whose runtime is an AI. Deterministic logic stays in Python-style code, while cognitive operations are marked with the `AI_` prefix. Together they form one executable specification that an AI reads, understands, and carries out.

## Quick Start

1. Decompose the task with **Gantree** using 4-space indentation.
2. Describe only complex nodes with full **PPR `def`** blocks.
3. Use the **`AI_`** prefix where judgment is required; use real code where exactness matters.
4. Embed completion rules with **`acceptance_criteria`**.
5. Execute, verify, and rework only when needed.

```text
MyTask // task description (in-progress)
    StepA // first step (done)
    StepB // second step (in-progress) @dep:StepA
        # input: data from StepA
        # process: AI_analyze(data) -> result
        # criteria: accuracy >= 0.9
```

## Load-Bearing Properties

- **Parser-free**: PG is built from structures an AI already understands, such as Python syntax, indentation trees, and function composition. It does not need a parser or compiler.
- **Co-evolutionary**: better models produce better results from the same PG document, while sharper specifications improve execution quality.
- **AI-native runtime**: `AI_` functions are intentional non-deterministic operations, not bugs.
- **AI-to-AI communication layer**: intent, structure, procedure, state, and verification live directly in PG syntax rather than being hidden inside plain prose.

## Enforcement Caveat

PG is comprehension-based, not enforced. The same AI that can follow the notation can also silently drift from it.

Required discipline:

- update node status immediately after execution
- actually evaluate `acceptance_criteria`
- avoid using `AI_` where exact computation is required
- use external gates such as tests, validators, or reviews whenever possible

The caveat is not a flaw in the notation. It is the operating condition.

## Gantree

Use an indentation tree to decompose the system.

### Node Syntax

```text
NodeName // description (status) [@v:version] [@dep:dependency] [#tag]
```

- `NodeName`: CamelCase identifier
- `// description`: natural-language description
- `(status)`: execution state
- `@v:X.Y`: version tag, typically on the root
- `@dep:A,B`: dependency list
- `#tag`: classification tag
- `[parallel]...[/parallel]`: parallel execution section

### Status Codes

| Status | Meaning | Execution rule |
|--------|---------|----------------|
| `(done)` | completed | skip |
| `(in-progress)` | spec is complete and executable | run the PPR block |
| `(designing)` | spec is incomplete | allow only stub or placeholder logic |
| `(blocked)` | cannot proceed | skip and record why |
| `(decomposed)` | split because depth exceeded | refer to the split tree |
| `(needs-verify)` | executed but not yet verified | verify, then move to `done`, `designing`, or `blocked` |

### `designing` vs `in-progress`

Ask one question: can this node be implemented now from its PPR alone?

- Yes: mark it `in-progress`.
- No: keep it `designing`.

Useful test: if you handed the node to another runtime, would it implement it without follow-up questions?

### Structural Rules

- 4 spaces per level, no tabs
- maximum depth is 5; deeper trees must be split with `(decomposed)`
- if a node has 10 or more children, introduce a grouping node
- do not nest `[parallel]`
- do not place `@dep:` inside `[parallel]`

### Atomic Node and the 15-Minute Rule

Treat a node as atomic when it can realistically be completed by one AI runtime in about 15 minutes. Helpful heuristics:

- input and output are clear
- one responsibility only
- one compact implementation unit
- no meaningful benefit from further decomposition
- low external dependency count

The final rule is still the 15-minute completion test.

## PPR

PPR is the detailed intent specification layer built on Python-like syntax.

### Types

```python
text: str
user: dict = {"name": str, "age": int}
status: Literal["draft", "review", "published"]
nickname: Optional[str]
Section = dict[str, str | list[str] | int]
```

### Differences from Python

- `AI_` declares cognitive operations
- pipelines express flow
- `[parallel]` blocks express independent work
- type syntax is relaxed to communicate intent
- imports may be omitted

### `AI_` Functions

```python
def AI_[verb]_[target](params: Type) -> ReturnType:
    """Intent description"""
```

Use them for judgment, reasoning, perception, and creation.

Critical rule: use real code for exact computation.

```python
result = AI_calculate(2 + 2)        # wrong
result = 2 + 2                      # correct
analysis = AI_analyze_trend(sales)  # appropriate
```

If you explicitly need causative semantics, `AI_make_` is available but rare.

### Pipelines

```python
raw -> AI_clean -> AI_extract -> AI_classify -> result

input -> {
    "sentiment": AI_analyze_sentiment -> score,
    "keywords": AI_extract_keywords -> words,
}

[parallel]
tech = AI_analyze(data, lens="tech")
market = AI_analyze(data, lens="market")
[/parallel]
synthesis = AI_synthesize(tech, market) -> result
```

If a stage fails, stop the pipeline and keep the last successful output unless explicit recovery logic says otherwise.

### Convergence Loop

```python
draft = AI_generate(brief)
while True:
    evaluation = AI_evaluate(draft, criteria)
    if evaluation.score >= threshold:
        break
    draft = AI_revise(draft, evaluation.feedback)
```

### Failure Strategy

```python
for attempt in range(max_retry):
    result = AI_execute(task)
    if AI_verify(result, acceptance_criteria):
        return result
    if attempt >= 1:
        task.ppr = AI_redesign(task, result.failure_reason, constraint="preserve_public_interface")
task.status = "blocked"
```

### `acceptance_criteria`

```python
def some_task(input: InputType) -> OutputType:
    """Task description"""
    # acceptance_criteria:
    #   - all required fields are present
    #   - AI_assess_quality >= 0.85
    #   - output format is valid
```

## Linking Gantree and PPR

| Node type | Connection style | Size |
|----------|------------------|------|
| simple atomic node | inline `AI_extract_keywords` style | single call |
| compact PPR | 3-7 `#` lines below the node | small |
| full PPR | dedicated `def` block | medium and above |

## Progressive Formalization

| Level | Form | Best for |
|---|---|---|
| 1 | one-line natural language | tiny fixes and settings |
| 2 | Gantree with inline comments | features and refactors |
| 3 | Gantree plus full PPR and acceptance criteria | system design and large work |

Promote upward when complexity grows.

## When You Encounter a PG Document

1. Read the Gantree structure.
2. Respect `(status)`.
3. Follow `@dep:`.
4. Run `[parallel]` as independent work.
5. Interpret PPR `def` blocks.
6. Interpret inline `#` PPR when no full `def` exists.
7. Execute inline `AI_` expressions directly.
8. Recurse into child nodes when needed.

## Checklist

- Gantree: depth within limits, status defined, decomposition complete, no cyclic dependencies, correct indentation.
- PPR: complex nodes have defs, I/O types are clear, `AI_` names use snake_case, deterministic logic stays deterministic.
- Common mistakes: Gantree without PPR for complex work, putting all logic in the tree, exceeding depth limits, using `AI_` for exact arithmetic.
