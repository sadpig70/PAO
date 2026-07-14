---
name: pgxf
description: "PGXF (PPR/Gantree IndeX Framework) — file-based index system for large PG structures. Enables lazy-load subtree access, node lookup, cross-file status aggregation, and synchronization of decomposed trees. Triggers: index, large-scale design, node lookup, structural overview, status aggregate, pgxf."
user-invocable: true
argument-hint: "build|lookup|sync|status|prune [project-name|node-name]"
---

# PGXF — PPR/Gantree IndeX Framework v1.1

> If PG is the language and PGF is the library, PGXF is the **filesystem index**. It enables precise access to required nodes without loading the full tree into context.

## When to Use It, and When Not To

PGXF only pays for itself at scale. On small projects it is overhead.

Use it when at least one condition is true:

- there are **30+ nodes**
- `.pgf/` spans **multiple files** or uses `(decomposed)` splits
- you need repeated **status aggregation** or repeated "where is this node?" lookups

Do not use it when:

- the tree is **under 30 nodes in one file** and fits in context
- the task is a one-off read where index maintenance costs more than it saves

Honest threshold rule: at 23 nodes, PGXF's main advantage, lazy-load access, usually does not matter. Below threshold, `pgf status` is enough.

## Core Concept

### IndexEntry

```python
IndexEntry = {
    "node": str, "status": str, "file": str, "line": int, "depth": int,
    "parent": Optional[str], "children": list[str], "deps": list[str],
    "has_ppr": bool, "ppr_file": Optional[str], "ppr_line": Optional[int],
    "decomposed_to": Optional[str], "tags": list[str],
}
```

### Paths

```text
<root>/.pgf/      # PGF sources (DESIGN/WORKPLAN/status)
<root>/.pgxf/
    INDEX-{Name}.json   # project index, always rebuildable
    MANIFEST.json       # optional multi-project aggregate
```

`.pgxf/` sits beside `.pgf/`. The index is a **derived artifact**. Keeping it in git is a policy choice; `.gitignore` is usually better.

## Modes

| Mode | Trigger | Action |
|------|---------|--------|
| `build` | "build index" | Scan `.pgf/` DESIGN and WORKPLAN files, then create `INDEX-{Name}.json` |
| `lookup` | "find node", "where is it" | Resolve node name to file:line, PPR location, status, parent, children, and deps |
| `sync` | "sync", "refresh index" | Full rebuild, then report added, removed, and modified entries |
| `status` | "status overview" | Aggregate state from the index and summarize the tree |
| `prune` | "remove deleted nodes" | Remove orphaned entries no longer present in source files |

## Build and Sync Procedure

The AI runtime can execute the procedure below deterministically even without a dedicated script. Same input, same index.

```python
def pgxf_build(project: str) -> Index:
    sources = list_pgf_files(project)

    nodes = {}
    for src in sources:
        for ln, raw in enumerate(read_lines(src), start=1):
            if not is_gantree_node(raw):
                continue
            depth = leading_spaces(raw) // 4
            name = parse_node_name(raw)
            status = parse_status(raw)
            nodes[name] = {
                "node": name,
                "status": status,
                "file": src,
                "line": ln,
                "depth": depth,
                "parent": stack_parent(depth),
                "children": [],
                "deps": parse_deps(raw),
                "has_ppr": False,
                "ppr_file": None,
                "ppr_line": None,
                "decomposed_to": resolve_decomposed(raw) if status == "decomposed" else None,
                "tags": parse_tags(raw),
            }
    link_parent_children(nodes)
    map_ppr_defs(nodes, sources)
    save_json(f".pgxf/INDEX-{project}.json", index(nodes))
    return index
    # acceptance_criteria:
    #   - node names are unique; duplicates produce warnings with file and line
    #   - parent/child links are bidirectionally consistent
    #   - decomposed nodes point to real split files
    #   - identical input yields a byte-stable index
```

```python
def pgxf_sync(project: str) -> Diff:
    old = load_index(project)
    new = pgxf_build(project)
    return diff(old, new)
```

When node state changes, a manual `sync` is recommended. At 50+ nodes, check `sync` before `lookup`.

## Lookup and Status Output

```text
[PGXF] PaymentProcessor
  file: .pgf/DESIGN-OrderSystem.md:45   status: in-progress
  PPR: :112 (def payment_processor)   parent: OrderSystem   children: ValidateCard, ChargeCard   deps: UserAuth
```

```text
[PGXF] OrderSystem status   done 9/15 (60%) | in-progress 3 | designing 2 | blocked 1
  blocked: PaymentGateway (external API)
  decomposed: ShippingFlow → DESIGN-ShippingFlow.md
```

## Automatic Tracking for `(decomposed)`

PGXF's main value is crossing file boundaries. For a `(decomposed)` node, the index includes children from the split file.

```text
# DESIGN-OrderSystem.md
PaymentFlow // payment flow — see DESIGN-PaymentFlow.md (decomposed)
# DESIGN-PaymentFlow.md
PaymentFlow // payment details (in-progress)
    ValidateCard // (done)
```

The index then records `PaymentFlow.children = [ValidateCard, ...]` and `decomposed_to = ".pgf/DESIGN-PaymentFlow.md"`.

## Lazy-Load Pattern

```python
index = pgxf_load_index(project)
targets = AI_identify_relevant_nodes(task, index.summary)
for n in targets:
    load_file_range(index.nodes[n].file, index.nodes[n].line, estimate_end(...))
execute_task(task)
pgxf_sync(project)
```

## PGF Integration

| PGF event | PGXF action |
|------------|-------------|
| `design` complete | `build` when above threshold |
| `execute` state change | `sync` recommended |
| `loop` start | load the index before execution |
| `full-cycle` complete | `sync` and `status` |
| `(decomposed)` created | automatically tracked on next `sync` |

## Checklist

- Build: scan every DESIGN and WORKPLAN file; detect duplicate node names; validate decomposed targets; map PPR defs correctly; keep parent and child links consistent.
- Sync: report accurate added, removed, and modified counts; keep summary counts aligned with node state; validate `decomposed_to`.
- Operations: treat INDEX as derived data; use MANIFEST only for multi-project setups; run `sync` before lookup at 50+ nodes; skip PGXF entirely for single-file trees under 30 nodes.
