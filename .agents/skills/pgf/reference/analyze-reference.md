# Design Analyze Mode — Existing Codebase Reverse Engineering Specification

**Scans a codebase to automatically generate a PGF design document.** Directory structure → module boundaries → dependencies → DESIGN-{Name}.md output.

---

## 1. Overview

### Purpose

Reverse engineer an existing codebase into a PGF design document. Read code, identify logical module boundaries, extract the dependency graph, and automatically generate a DESIGN document containing Gantree + PPR stubs.

### Use Cases

| Case | Description |
|------|------|
| Legacy code analysis | Visualize undocumented existing code structure as PGF |
| Onboarding structure comprehension | Generate a design map for rapid understanding of the entire codebase |
| Refactoring planning | Determine refactoring scope by identifying module boundaries and dependencies |
| PGF workflow connection | Convert generated DESIGN document to execution via `/PGF workplan` |

---

## 2. Commands

| Command | Action |
|------|------|
| `/PGF design --analyze {project_name}` | Full analysis based on current directory |
| `/PGF design --analyze {project_name} --root {path}` | Analysis based on specified path |
| `/PGF design --analyze {project_name} --depth {N}` | Limit Gantree maximum depth |
| `/PGF design --analyze {project_name} --focus {module}` | Focused analysis on a specific module |

### Options

| Option | Default | Description |
|------|--------|------|
| `--root PATH` | Current directory | Root path of analysis target |
| `--depth N` | 5 | Gantree maximum depth limit. Prevents excessive decomposition |
| `--focus MODULE` | (none) | Deep analysis of a specific module/directory only |

---

## 3. Execution Process

Executes 5 Phases sequentially. Progress report is output upon each Phase completion.

### Phase 1: Directory Structure Scan

```python
def scan_directory_structure(root_path: str, depth: int) -> DirectoryTree:
    """Directory structure → Gantree draft mapping

    1. Collect full file list under root_path
    2. Filter ignored targets
    3. Convert directory hierarchy → Gantree draft
    """
    tree = Glob(f"{root_path}/**/*")

    # Ignored targets: node_modules, .git, __pycache__, dist, build,
    #                  .next, .venv, venv, .tox, coverage, .idea, .vscode, etc.
    filtered = AI_filter_relevant_paths(tree)

    return AI_map_to_gantree_draft(filtered, depth)
```

**AI_filter_relevant_paths criteria:**
- Build artifacts, package manager caches, IDE settings → excluded
- If `.gitignore` exists, its patterns are also applied
- Test/documentation directories → included (needed for module structure comprehension)

### Phase 2: Module Boundary Identification

```python
def identify_modules(root_path: str, draft_tree: DirectoryTree) -> list[Module]:
    """Read code → identify logical module boundaries

    1. Select entry points, configuration files, core modules
    2. Read selected file contents
    3. Determine logical module boundaries and consolidate
    """
    key_files = AI_select_key_files(draft_tree)
    # Priority: entry points (main, index, app) > config (config, settings)
    #           > package definitions (package.json, pyproject.toml, Cargo.toml)
    #           > core business logic

    for f in key_files:
        content = Read(f)
        AI_analyze_module_boundary(content)
        # Analyze export/import patterns, class hierarchies, namespaces

    return AI_consolidate_modules(draft_tree)
```

**Module identification criteria:**
- Directory-level modules (most common)
- Single-file modules (independent functional units)
- Logical modules (multiple files composing a single feature)

### Phase 3: Dependency Analysis

```python
def extract_dependencies(modules: list[Module]) -> DependencyGraph:
    """Inter-module dependencies → @dep: graph

    1. Extract static dependencies from import/require/use statements
    2. Extract cross-module references from function call graphs
    3. Consolidate dependency direction and strength into a graph
    """
    imports = AI_extract_import_graph(modules)
    # Python: import/from, JS/TS: import/require, Rust: use/mod,
    # Go: import, Java: import, C#: using

    calls = AI_extract_call_graph(modules)
    # Track function/method calls crossing module boundaries

    return AI_build_dependency_graph(imports, calls)
```

**DependencyGraph output format:**
```text
@dep: ModuleA → ModuleB   # Direct dependency
@dep: ModuleA → ModuleC   # Direct dependency
@dep: ModuleB → ModuleC   # Indirect via
```

### Phase 4: PPR Stub Generation

```python
def generate_ppr_stubs(modules: list[Module], deps: DependencyGraph) -> list[PPRDef]:
    """Generate PPR def draft for each module

    Only generate PPR def blocks for complex modules (not atomizable).
    Simple utility/helper modules need only Gantree nodes.
    """
    stubs = []
    for module in modules:
        if AI_is_complex_module(module):
            # Complexity criteria: file count, class count, external dependency count,
            #                      branching complexity, state management presence
            stub = AI_generate_ppr_def(module)
            # def signature + core flow + AI_ operation identification
            stubs.append(stub)
    return stubs
```

**PPR stub includes:**
- `def` signature (input/output types)
- Core processing flow (main branches, loops)
- Points where AI cognitive operations are needed
- `@dep:` dependency annotations

### Phase 5: DESIGN Document Output

```python
def write_design(project_name: str, modules, deps, stubs) -> str:
    """Generate DESIGN-{Name}.md file

    Integrate Gantree structure + dependency graph + PPR stubs
    to output a PGF standard DESIGN document.
    """
    path = f".pgf/DESIGN-{project_name}.md"

    content = format_design_md(
        gantree=modules_to_gantree(modules),
        dependencies=deps,
        ppr_stubs=stubs,
        metadata={
            "generated_by": "design --analyze",
            "source_root": root_path,
            "analysis_depth": depth,
        }
    )

    Write(path, content)
    return path
```

---

## 4. Integrated PPR

```python
def analyze_codebase(root_path: str, project_name: str, depth: int = 5,
                     focus: Optional[str] = None) -> str:
    """Existing codebase → DESIGN-{Name}.md reverse engineering

    Executes 5 Phases sequentially to automatically generate a PGF design document.
    Progress report is output upon each Phase completion.
    """
    # Phase 1: Directory structure scan
    dir_tree = scan_directory_structure(root_path, depth)
    report("[PGF ANALYZE] Phase 1/5 Directory scan", f"{len(dir_tree.files)} files found")

    # Phase 2: Module boundary identification
    if focus:
        dir_tree = filter_tree_by_focus(dir_tree, focus)
    modules = identify_modules(root_path, dir_tree)
    report("[PGF ANALYZE] Phase 2/5 Module identification", f"{len(modules)} modules identified")

    # Phase 3: Dependency analysis
    deps = extract_dependencies(modules)
    report("[PGF ANALYZE] Phase 3/5 Dependency analysis", f"{len(deps.edges)} dependencies mapped")

    # Phase 4: PPR stub generation
    stubs = generate_ppr_stubs(modules, deps)
    report("[PGF ANALYZE] Phase 4/5 PPR generation", f"{len(stubs)} def blocks created")

    # Phase 5: DESIGN document output
    design_path = write_design(project_name, modules, deps, stubs)
    report("[PGF ANALYZE] Phase 5/5 Document output", f"DESIGN-{project_name}.md")

    return design_path
```

---

## 5. Context Management

Large codebases cannot be fully read, so a selective analysis strategy is applied.

| Codebase Scale | Strategy |
|----------------|------|
| Small (< 50 files) | Full file reading possible |
| Medium (50~200 files) | Priority reading of core files + signatures only for the rest |
| Large (> 200 files) | `--focus` strongly recommended. Read only entry points + config + target modules |

### Core File Selection Priority

1. **Package definitions**: `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `*.csproj`
2. **Entry points**: `main.*`, `index.*`, `app.*`, `Program.*`
3. **Configuration**: `config.*`, `settings.*`, `.env.example`
4. **Routers/Handlers**: API endpoint definition files
5. **Models/Schemas**: Data structure definition files
6. **Core business logic**: Domain-specific judgment

### When Using `--focus`

```text
/PGF design --analyze MyProject --focus src/engine
  → Deep analysis of src/engine/** only
  → Other modules included at dependency reference level only
```

### Gantree Depth Limit

Prevent excessive decomposition with `--depth N`. Default 5 is suitable for most projects.

```text
depth=3: Project → Module group → Module (high-level overview)
depth=5: Project → Group → Module → Submodule → Component (standard)
depth=7: Decomposition to function/class level (recommended for small projects only)
```

---

## 6. Output Structure

Standard structure of the generated `DESIGN-{Name}.md`:

```markdown
# DESIGN-{Name}

> Generated by `/PGF design --analyze`
> Source: {root_path}
> Date: {date}

## Gantree

{Name}
├── {Module-A}
│   ├── {SubModule-A1}
│   └── {SubModule-A2}
├── {Module-B}
│   └── {SubModule-B1}
└── {Module-C}

## Dependencies

@dep: Module-A → Module-B
@dep: Module-B → Module-C

## PPR Definitions

### {Module-A}

​```python
def module_a_core(input: InputType) -> OutputType:
    """Module A core processing flow"""
    config = Read("config.yaml")
    data = AI_parse_input(input)
    result = process(data)
    return result
​```

### {Module-B}
...
```

---

## 7. Progress Report Format

Report in the following format upon each Phase completion:

```text
[PGF ANALYZE] Phase 1/5 Directory scan | 127 files found
[PGF ANALYZE] Phase 2/5 Module identification | 8 modules identified
[PGF ANALYZE] Phase 3/5 Dependency analysis | 12 dependencies mapped
[PGF ANALYZE] Phase 4/5 PPR generation | 5 def blocks created
[PGF ANALYZE] Phase 5/5 Document output | DESIGN-MyProject.md

[PGF ANALYZE] Complete → .pgf/DESIGN-MyProject.md
```

---

## 8. Limitations

| Limitation | Description |
|------|------|
| AI cognition-based analysis | 100% accuracy not guaranteed. Module boundary judgment is heuristic |
| Dynamic dependencies | Runtime binding, DI containers, reflection-based calls are difficult to detect |
| Draft output | Generated DESIGN document is a draft — user review and correction required |
| Binary/obfuscated | Compiled binaries and obfuscated code cannot be analyzed |
| Context window | For very large codebases, scope limitation with `--focus` is mandatory |

---

## 9. Connection with Other PGF Modes

| Follow-up Command | Action |
|-----------|------|
| `/PGF workplan {Name}` | Convert DESIGN-{Name}.md → WORKPLAN-{Name}.md |
| `/PGF execute {Name}` | Sequential node execution based on workplan |
| `/PGF design --analyze {Name} --focus {module}` | Re-analyze specific module (update DESIGN) |

Typical workflow:
```text
/PGF design --analyze MyProject                     # Comprehend overall structure
/PGF design --analyze MyProject --focus src/core     # Deep analysis of core module
→ Review/correct DESIGN-MyProject.md
/PGF workplan MyProject                              # Generate execution plan
/PGF execute MyProject                               # Execute
```
