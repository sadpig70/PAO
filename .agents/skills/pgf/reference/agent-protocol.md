# Agent Protocol — PG TaskSpec for Delegation

## Purpose

This reference defines how PGF expresses a task when one agent delegates work to another agent. The delegated task should preserve PG structure instead of collapsing into loose natural-language instructions.

## Core Rule

A delegated task should carry:

- one clear objective
- explicit context files to read
- acceptance criteria
- implementation guidance when needed
- failure behavior
- return structure

## Minimal TaskSpec Shape

```python
def DelegatedTask(input: InputType) -> OutputType:
    """One-line task objective"""
    # context:
    #   - Read(path_a)
    #   - Read(path_b)
    # acceptance_criteria:
    #   - condition A
    #   - condition B
    # implementation:
    #   - follow the required interface
    #   - preserve public behavior unless instructed otherwise
    # failure_strategy:
    #   - report blockers with evidence
    # return:
    #   - changed files
    #   - verification results
```

## Required Fields

| Field | Meaning | Required |
|------|---------|----------|
| function signature | input parameters and return type | yes |
| docstring | one-line task description | yes |
| `# context:` | files to read before work | yes |
| `# acceptance_criteria:` | completion gate | yes |
| `# steps:` | ordered execution guidance | optional |
| `# implementation:` | core logic hints | optional |
| `# failure_strategy:` | how to react to failure | optional |
| `# return:` | expected result structure | optional |

## Parallel Dispatch

Use `[parallel]` when multiple agents can work independently.

```python
[parallel]
DiscordAdapterTask(...)
SlackAdapterTask(...)
TelegramAdapterTask(...)
[/parallel]

def ValidateAllAdapters(...) -> ValidationResult:
    """Integration validation after all parallel tasks complete"""
```

Each parallel task should have its own TaskSpec. Integration happens only after all return.

## Dependency Chains

Use `@dep:` when delegated tasks must run in order.

```text
ExpandChannelTrait // extend the shared interface (in-progress)
ImplementAdapters // implement the concrete adapters (in-progress) @dep:ExpandChannelTrait
```

## Result Contract

Delegated agents should return structured results, not just prose.

```python
TaskResult = {
    "status": Literal["succeeded", "failed", "blocked"],
    "summary": str,
    "changed_files": list[str],
    "verification": list[str],
    "artifacts": list[str],
    "open_risks": list[str],
}
```

## Orchestrator Flow

When PGF encounters a delegatable branch:

1. extract a TaskSpec from the node or its PPR
2. preserve the PG structure in the delegated prompt
3. dispatch the task
4. wait for completion
5. collect TaskResults
6. re-check the original acceptance criteria

## Conversion Rule

The conversion from PG TaskSpec to agent prompt should preserve structure. Natural language is supporting text, not the payload itself.

## Practical Guidance

- use a compact TaskSpec for simple delegated work
- use a full TaskSpec for complex delegated work
- require typed result contracts for multi-agent parallel work
- skip TaskSpec entirely for small tasks that the current runtime can execute directly
