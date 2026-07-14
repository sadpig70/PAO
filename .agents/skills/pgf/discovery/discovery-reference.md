# Discovery Engine Execution Specification

## 1. Overview

Discovery Engine is ClNeo's discovery engine that automatically executes the IdeaFirst 7-stage pipeline using 8 PGF design personas.

### Core Principle

```
User: "/PGF discover" or "find me ideas"
    ↓
Load 8 personas from personas.json
    ↓
STEP 1~6: Run 8 Agents in parallel at each stage → Aggregate results → Feed into next stage
    ↓
STEP 7: Present final results to user
    ↓
Upon approval → Pass to PGF design engine (/PGF design)
```

---

## 2. File Structure

```
<skill-dir>/discovery/
    personas.json           ← 8 persona profile data
    discovery-reference.md  ← This document (execution specification)

<project-root>/.pgf/discovery/
    news.md                 ← STEP 1 output
    industry_trend.md       ← STEP 2 output
    insight.md              ← STEP 3 output
    system_design.md        ← STEP 4 output
    candidate_idea.md       ← STEP 5 output
    final_idea.md           ← STEP 6 output
    archive/
        YYYY-MM-DD/         ← Date-based archive
```

---

## 3. Commands

### `/PGF discover`

Runs the discovery engine to automatically traverse the IdeaFirst 7-stage pipeline.

**Options:**
- `--personas N`: Number of personas to use (default: 8, minimum: 2)
- `--from-step N`: Restart from a specific step (requires previous step outputs)
- `--output-dir PATH`: Output path (default: `.pgf/discovery`)

---

## 4. Execution Protocol

### 4.1 Initialization

1. Load `personas.json` (`{SKILL_DIR}/discovery/personas.json`) — metadata including 21 domain list
2. Create output directory (`.pgf/discovery/`)
3. If `--from-step` is specified, verify previous step outputs exist

### 4.2 Persona Agents and Prompt Configuration

The 8 personas are independently defined as agent files at `{SKILL_DIR}/agents/pgf-persona-p{1~8}.md`.
Each agent file has built-in system_prompt, model(sonnet), and allowed-tools, so only the task prompt needs to be passed when calling the Agent.

Prompt structure for each Agent call:

```text
## Task
{step_task_prompt}

## Input from Previous Stage
{prev_stage_content}     ← Omitted in STEP 1
```

**HAO Principle Compliance**: Do not enforce output format. Allow free expression from personas.

### 4.3 Parallel Agent Execution

At each stage, **issue 8 Agent tool calls simultaneously in a single message** for all 8 personas.

```
Agent(description="IdeaFirst P1 Disruptive Engineer", prompt=task_prompt, agent="{SKILL_DIR}/agents/pgf-persona-p1.md")
Agent(description="IdeaFirst P2 Cold-Eyed Investor", prompt=task_prompt, agent="{SKILL_DIR}/agents/pgf-persona-p2.md")
...
Agent(description="IdeaFirst P8 Convergence Architect", prompt=task_prompt, agent="{SKILL_DIR}/agents/pgf-persona-p8.md")
```

- Agent files have built-in `model: sonnet` + `allowed-tools` — no separate specification needed at call time
- `subagent_type`: `general-purpose` (requires WebSearch access)
- `run_in_background`: `false` (must wait for results before proceeding to next stage)

### 4.4 Result Aggregation

After each stage completes, aggregate 8 results into a single `.md` file:

```markdown
# {STEP Name} — Aggregated Results
Generated: {ISO date}
Personas: 8

---

## [P1] Disruptive Engineer
{P1's original result — preserved unedited}

---

## [P2] Cold-Eyed Investor
{P2's original result}

---
... (all 8)
```

Save the aggregated file to `.pgf/discovery/{step_output}.md` and pass the full content as `prev_stage_content` to the next stage.

---

## 5. 7-Stage Pipeline Details

### STEP 1: News Collection (news.md)

**Task Prompt:**
```
Collect the latest news, reports, and trends across these 21 domains
as of today's date. Select the 10 most important items based on YOUR
perspective and priorities.

Use WebSearch to find REAL, current information. Do not fabricate.

[21 Domains]
- AI
- Quantum Technology
- Space/Aerospace
- Semiconductor
- Cybersecurity
- Healthcare
- Policy/Regulation/Governance
- Education/Learning
- Environment/Climate
- Urban/Infrastructure
- Robotics
- Big Tech
- Finance
- Media/Content Platforms
- Internet/Network
- Energy
- Advanced Materials
- Pharma/Bio
- Markets
- Data Technology/Infrastructure
- Smart Home

For each item: source, date, summary, and YOUR analysis of why it matters
from your perspective.
```

**Input**: None (first stage)
**Output**: `.pgf/discovery/news.md`

### STEP 2: Trend Analysis (industry_trend.md)

**Task Prompt:**
```
Analyze the input news data across all 21 domains using these 4 perspectives:

(1) Technology Trends — what's accelerating, what's plateauing
(2) Market & Industry Structure Changes — who's winning, who's losing, new entrants
(3) Policy/Regulatory Changes — new laws, standards, governance shifts
(4) Short & Medium-term Risks and Opportunities — 1-3 year horizon

Focus on YOUR domain expertise. Provide YOUR unique analytical angle.
```

**Input**: Full text of news.md
**Output**: `.pgf/discovery/industry_trend.md`

### STEP 3: Insight Extraction (insight.md)

**Task Prompt:**
```
Extract 10 key insights from the trend analysis input.

For each insight:
- Which analyses led to this insight
- Why it matters (from technology, market, AND policy perspectives)
- What it implies for the next 2-5 years

Focus on CROSS-DOMAIN insights that connect multiple fields.
Prioritize insights that YOUR perspective uniquely reveals.
```

**Input**: Full text of industry_trend.md
**Output**: `.pgf/discovery/insight.md`

### STEP 4: Idea Generation (system_design.md)

**Task Prompt:**
```
Using the insights, generate 3 NEW system ideas that combine
different domains in unexpected ways.

Recommended structure (not mandatory — express in your own way):

[Insight Layer I] — Connected insights
[Hypothesis Layer H] — Logical interpretation from combining insights
[Creation Layer C] — Core concept, architecture, operating principle
[Scenario Layer S] — Future assumptions (optional)

CRITICAL: Each idea MUST combine at least 2 different domains.
Express your UNIQUE perspective. Be bold from YOUR viewpoint.
```

**Input**: Full text of insight.md
**Output**: `.pgf/discovery/system_design.md` (8x3 = 24 ideas)

### STEP 5: Top Selection (candidate_idea.md)

**Task Prompt:**
```
Evaluate ALL system ideas from the input and select the TOP 3
most valuable ones from an investor-engineer perspective.

Selection criteria:
(1) Feasibility — technical realizability
(2) Impact — industry/market disruption potential
(3) Integrity — logical consistency
(4) Novelty — innovation level

For each selection: explain WHY from YOUR perspective.
Apply YOUR evaluation bias — it's deliberate, not a flaw.
```

**Input**: Full text of system_design.md
**Output**: `.pgf/discovery/candidate_idea.md`

### STEP 6: Final Selection (final_idea.md)

**Task Prompt:**
```
Evaluate ALL candidate ideas and select THE SINGLE BEST idea of today.

Final selection criteria:
- Cross-domain fusion depth
- 2026-2030 realizability
- Technology/market/policy/social impact
- Creative Emergence (emergent properties from combination)
- Long-term extensibility

For the selected idea, provide:
- Selection rationale
- 5 key strengths
- 3 potential risks
- Future expansion scenarios

If you genuinely cannot choose one, select your top 2 with equal rationale.
```

**Input**: Full text of candidate_idea.md
**Output**: `.pgf/discovery/final_idea.md`

**Convergence Judgment**: Analyze the final selections of 8 personas:
- Majority (5+) agreement → `CONVERGED` — Selected idea is finalized
- Below majority → `DIVERGED` — Present all top candidates to user

### STEP 7: User Verification (discover mode)

Report final results to user:

```
[Discovery Engine] Discovery Complete

{final_idea.md summary}

Convergence Status: {CONVERGED | DIVERGED}

Next Steps:
1. Select idea → Pass to PGF design engine (/PGF design {idea_name})
2. Re-run → Re-explore from different perspectives (/PGF discover)
3. Archive → Store by date
```

### STEP 7 Alternative: Automatic Selection (create mode)

When `/PGF create` is executed, STEP 7 replaces user verification with **automatic idea selection**:

1. **Extract final selections from 8 personas**: Parse each persona's selected idea from final_idea.md
2. **Vote tally**: Count votes per idea
3. **Selection decision**:
   - Majority (5+) agreement → `CONVERGED` — Immediately select the consensus idea
   - Below majority → `DIVERGED_AUTO_SELECTED` — Select the idea with most votes
   - Tie → Select the idea with highest novelty x impact weighted score
   - 0 votes (extraction failure) → Halt, request manual selection from user
4. **Record selection rationale**: Log vote results, selection rationale, convergence status in `.pgf/discovery/creation_log.md`
5. **Immediately transition to next Phase**: Auto-feed selected idea into Phase 2 (DESIGN)

```
[ClNeo CREATE] Phase 1 DISCOVER complete
  Selection: "{idea_name}" | consensus: CONVERGED | votes: 6/8
  → Auto-transitioning to Phase 2 DESIGN
```

---

## 6. Archive

Store outputs by date after each execution completes:

```
.pgf/discovery/archive/YYYY-MM-DD/
    news.md
    industry_trend.md
    insight.md
    system_design.md
    candidate_idea.md
    final_idea.md
    creation_log.md          ← Only exists in create mode
```

If a folder for the same date already exists, append sequential number as `YYYY-MM-DD_N` (N=2,3,...).

### Archive Script

Auto-archive via `{SKILL_DIR}/discovery/archive-discovery.py`:

```bash
# Invocation — script operates on .pgf/discovery relative to cwd
python "{SKILL_DIR}/discovery/archive-discovery.py"

# To archive from a different project root, cd there first
cd "<project-root>" && python "{SKILL_DIR}/discovery/archive-discovery.py"
```

### Archive Trigger Rules

| Mode | Trigger Timing | Auto/Manual |
|------|---------------|-------------|
| `/PGF discover` | After STEP 7 user verification completes | Manual (after user confirmation) |
| `/PGF create` | Immediately after Phase 1 DISCOVER completes | Auto (before transitioning to next Phase) |

---

## 7. Context Management

Aggregated results from each stage (`news.md`, etc.) can reach thousands of lines. Context limit management:

1. **STEP 1~3**: Pass full text of previous stage as `prev_stage_content` (prevent information loss)
2. **STEP 4 onwards**: If previous stage results are excessively long, create and pass a key summary
3. **No `/compact` between stages**: Pipeline continuity must be maintained
4. **Output files always store full text**: Summaries are only for Agent delivery; files preserve originals

---

## 8. Error Recovery

| Situation | Response |
|-----------|----------|
| 1~3 Agents fail (timeout, etc.) | Re-run only failed personas, preserve remaining results |
| 4+ Agents fail (below majority) | **Invalidate stage** → Re-run all 8 personas |
| 5+ Agents fail (majority failure) | **Halt stage** → Report error to user, stop pipeline |
| Session disconnected mid-stage | Restart from next stage after last completed using `--from-step N` |
| WebSearch inaccessible | Mark "[WebSearch unavailable]" in that persona's result, continue to next stage |
| Context limit reached | Save outputs up to current stage → `/compact` → Resume with `--from-step` |

### Minimum Quorum Rule

Each stage's validity requires **at least 4 personas' successful results**:

- 4+ successes → Stage valid, proceed to next stage
- 3 or fewer successes → Stage invalid, re-run or halt
- Quorum still not met after re-run → Halt + report to user

```text
[Discovery Engine] ✗ STEP {N} halted
  Successful Agents: {count}/8 (below minimum quorum of 4)
  Failed Agents: {failure list}
  Reason: {timeout/error details}
  → Manual intervention required
```

---

## 9. Design Document Reference

The full PGF design (Gantree + PPR def blocks) is located at `.pgf/DESIGN-DiscoveryEngine.md` in the project.

---

## 10. Agent Teams Mode (Experimental)

### Overview

An alternative execution mode leveraging Claude Code's Agent Teams feature for **real-time cross-pollination** between personas.

### Standard vs Agent Teams Execution

| Aspect | Standard (Subagent) | Agent Teams |
|--------|---------------------|-------------|
| Communication | One-way (persona → main) | Peer-to-peer (persona ↔ persona) |
| Cross-pollination | Stage-level (via aggregated results) | Real-time (mid-stage messaging) |
| Coordination | Main agent aggregates | Team lead orchestrates |
| Cost | Lower (parallel subagents) | Higher (persistent sessions) |
| Quality | Good diversity | Potentially deeper synthesis |

### When to Use Agent Teams

- When deeper cross-domain synthesis is desired
- When persona interactions might produce emergent insights
- For STEP 3 (Insight) and STEP 4 (Idea Generation) where cross-pollination is most valuable
- Not recommended for STEP 1 (News Collection) — independent search is sufficient

### Hybrid Approach (Recommended)

```
STEP 1-2: Standard subagent mode (independent, parallel)
    ↓ (results aggregated as usual)
STEP 3-4: Agent Teams mode (peer-to-peer interaction enabled)
    ↓ (richer cross-domain insights via real-time dialogue)
STEP 5-6: Standard subagent mode (independent evaluation)
```

This hybrid maximizes cross-pollination where it matters most (insight/ideation) while keeping independent evaluation intact (selection/voting).

### Invocation

```
/PGF discover --teams        # Full Agent Teams mode
/PGF discover --teams-hybrid # Hybrid: Teams for STEP 3-4 only (recommended)
```

### Implementation Note

Agent Teams is an experimental Claude Code feature. Fallback to standard subagent mode if Agent Teams is unavailable or encounters errors.
