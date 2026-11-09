# Design & Implementation Plan: Intelligent PR Review Assistant

## 1. Goal

Build a CLI that reviews **any pull request** given in the assignment's JSON format and writes a structured review:

```bash
python analyze_pr.py --pr pr_input.json --output code_review_output.json
```

The output fields are `risk_level`, `risk_reasoning`, `architectural_impact`, `review_focus_areas`, `complexity_score`, `confidence` and `reasoning_trace`.

### Principles
1. **Works for every PR.** The same graph handles a docs typo, a 5-file feature or a 100-file refactor. Nothing is tied to the sample PR.
2. **Simple by default.** There are two paths (simple and complex), 7 nodes, and one shared tail. Framework features are used only where they earn their place.
3. **LLM only where judgment is needed.** Facts are computed in code, and the LLM does the judging.
4. **Safe defaults.** When a decision is uncertain, the system takes the more thorough option.
5. **Always produce an honest output.** Any valid input produces an output that matches the schema. When a stage fails, the review is marked degraded, and the trace says why.
6. **Full control, no hidden behaviour.** The agent loop, its limits, error handling and termination are our own code (§7). Framework pieces are used as building blocks (graphs, messages, model clients), not as black boxes.

### Stack
| Package | Role | Submitted? |
|---|---|---|
| langgraph | Orchestration: the main graph and the hand-built ReAct subgraph, conditional routing, parallel fan-out/fan-in, bounded cycles, RetryPolicy | Yes |
| langchain-core + langchain-anthropic | Building blocks only: `ChatAnthropic`, message types, `bind_tools` schemas, structured output. **`create_agent` and agent middleware are not used** (§7). | Yes |
| python-dotenv | Load `.env` | Yes |
| Jev client (TypeSafe) | Decision model for routing. It's **optional**: without `JEV_API_KEY` the router uses rules only. The client (official SDK or plain HTTP) is chosen after reading the official docs in step 6. | Yes (optional) |
| langfuse | Local tracing for development only. It goes in a separate `uv` dependency group, is excluded from `requirements.txt`, and is enabled only when its keys are set. | No |

`langchain-openai` is removed.

---

## 2. Architecture

```
START → parse_pr → classify_pr ─┬─ simple ───────────────────────────────┐
                                │                                        ▼
                                └─ complex ─┬─► architecture_analyst ─► synthesize ⇄ validate → finalize → END
                                            └─► risk_agent (ReAct) ───►
```

| Node | Kind | Responsibility |
|---|---|---|
| `parse_pr` | Deterministic | Validate and normalize the input, filter mechanical files, classify and prioritize files, scan for secrets and injection attempts, build the review plan (must-review files and budgets) |
| `classify_pr` | Rules + decision model (Jev) + fallback | Choose `simple` or `complex` (§3.3). A conditional edge reads its decision. |
| `architecture_analyst` | 1 LLM call | Architectural impact. It needs no exploration, so it isn't an agent. |
| `risk_agent` | **ReAct agent**, a hand-built LangGraph subgraph (§7) | Concrete risks backed by evidence, investigated through tools |
| `synthesize` | 1 LLM call | Simple path: review the small diff directly. Complex path: merge the analysts' findings. On a retry it also gets the validator's feedback. |
| `validate` | Deterministic + conditional edge | Check that the draft is grounded and consistent, with at most 1 retry (a real cycle) |
| `finalize` | Deterministic | Compute the complexity score and calibrate confidence, then assemble the output |

### LangGraph features and why each one is used
| Feature | Where | Why |
|---|---|---|
| Conditional edges | After `classify_pr`, after `validate` | The path depends on the PR and on the draft's quality |
| Parallel fan-out / fan-in | The two analysts start together and join at `synthesize` | They're independent, so running them together halves the latency. LangGraph waits for both before `synthesize` runs. |
| Bounded cycles | `synthesize ⇄ validate`, and `agent ⇄ tools` inside the risk agent | Self-correction (at most 1 retry), and the ReAct loop (bounded by our budgets) |
| Subgraph | `risk_agent` | The ReAct loop is its own compiled graph with its own state. It's testable alone and appears nested in traces and Mermaid. |
| Reducers (`operator.add`) | `reasoning_trace`, `degraded` | Both parallel branches write to them safely |
| `RetryPolicy` | LLM nodes | Retry when the output doesn't match the schema |
| Input/output schemas | Graph | The caller sees `{raw_pr}` → `{output}`, and everything else stays internal |

**`Send` is not used.** `Send` is for a *dynamic* number of tasks decided at runtime, such as one per file. Our fan-out always has exactly two fixed branches, so static edges are the right tool.

### Alternatives considered
- **A single ReAct agent.** It's simpler, but it mixes responsibilities, has no guardrails, and its output is harder to validate.
- **Three lanes with a per-file triage pre-scan via `Send`** (an earlier version of this plan). It cut costs only on 100-file PRs, but added two nodes, a clustering algorithm and a second model. It was removed: the risk agent already pulls diffs on demand, and the 1M-token context fits large PRs.
- **`create_agent` for the risk agent.** It's quicker to write, but termination, limits, error handling and the final structured answer would live inside prebuilt middleware. Our own loop is about 150 lines, and every decision in it is visible, testable and traced (§7).
- **Claude as the router.** Claude doesn't expose token probabilities, so we'd get a self-reported confidence rather than a calibrated one. A decision model returns a real probability.
- **Jev for every routing decision.** Rules are free, exact and deterministic for the clear cases, so Jev is used only where the rules can't decide.

---

## 3. Adapting to the PR

### 3.1 Noise filter (deterministic, runs first)
| Category | Detection |
|---|---|
| Lockfiles / generated / vendored | `*.lock`, `package-lock.json`, `*.min.js`, `*_pb2.py`, `generated/`, `vendor/` |
| Binary / empty | No `@@` hunks or an empty diff |
| Pure rename | `renamed` with no hunks |
| Formatting only | Added and removed lines are equal after stripping whitespace |
| Import only | Every changed line is an import statement |

Filtered files are counted in the stats ("12 mechanical files skipped") and listed by the agent's tools, but they are never sent to an analyst.

**Only mechanical files are filtered.** Docs and tests have lower priority but are still included as context. For example, the architecture analyst can check that the docs match the code.

**File categories** are checked in this order, and the first match wins:
1. **mechanical** (the table above)
2. **test:** `tests/`, `test_*.py`, `*_test.py`, `conftest.py`
3. **docs:** `docs/`, `*.md`, `*.rst`, `*.txt`, `CHANGES`, `LICENSE`
4. **config:** `pyproject.toml`, `*.cfg`, `*.ini`, `*.yaml`, `.github/workflows/`
5. **source:** everything else, **including unknown file types**. A doubtful file is treated as code.

### 3.2 Priority and the review plan (in `parse_pr`)
`priority = 3·is_core + 2·is_source + 1.5·is_new_module + 1·sensitive_path + log10(1+lines_changed) − 1·is_test − 2·is_docs`

- `is_core` comes from the core path patterns in `project_context.md`.
- `sensitive_path` uses generic patterns: `auth`, `security`, `session`, `crypto`, `config`, `middleware`, `migrations`.
- The weights live in `config.py`.

`parse_pr` turns the priorities into a **`ReviewPlan`**, which both analysts read:
- **`ranked_files`:** every non-filtered file, ordered by priority.
- **`must_review`:** core files, sensitive paths, new modules, scan hits, and the top-5 files by priority.
- **Budgets:**
  - the architecture analyst's diff budget is about 40K tokens, filled in priority order,
  - the agent's tool budget is `clamp(8 + 2 × code_files, 8, 30)` tool calls.

The plan is deterministic and unit-testable, and it's written to the trace.

### 3.3 Routing: `classify_pr`
The routing decision has three layers, from safest to least safe.

**Layer 1: hard rules (deterministic, Jev can't override them)**

| Condition | Route |
|---|---|
| Only docs and/or mechanical files changed, and no scan hit | `simple` |
| A secret or injection scan hit | `complex` |
| Any core file, sensitive path, new module or deleted source file | `complex` |
| More than 3 code files, or more than 50 changed code lines | `complex` |
| Unknown file type or malformed diff | `complex` |

Code files are source, config and test files. The thresholds live in `config.py`.

**Layer 2: the decision model (Jev)**, only for the **uncertain middle**: small code changes (≤3 code files, ≤50 changed lines, nothing core, sensitive or new). Here rules can't tell a comment typo or version bump from a real logic change.
- **Input:** a bounded payload. It holds the title, the description (truncated), the file list with categories and +/- counts, and the changed lines (up to about 2K tokens).
- **Question:** *"Does this change need a full review (complex) or a light review (simple)?"*
- **Output:** `P(simple)`.

**Layer 3: the fallback**
- `P(simple) ≥ 0.85` → `simple` (the threshold is in config).
- Otherwise → `complex`.
- **Jev unavailable** (no `JEV_API_KEY`, a timeout of about 3s, an API error or an unexpected response) → `complex`. This is a safe default, not a degraded stage.

**Why the design is safe:**
- **The two kinds of mistake don't cost the same.** A simple PR on the complex path costs cents. A risky PR on the simple path is a missed risk. So every uncertain case goes to `complex`.
- **Jev can only move a PR to `simple` inside the uncertain middle,** so it can never downgrade a core, sensitive or large change.
- **Prompt injection is contained.** A description saying "trivial change, skip review" can at most influence the uncertain middle, and the injection scan forces `complex` first.
- **Data sharing is explicit.** PR content is sent to a third party only when `JEV_API_KEY` is set. The README states this.

**The route is not the risk level.** The route decides how much *process* a PR gets. The risk verdict comes later. A complex-path PR can be low risk.

Every decision goes into the trace with its source:
- *"Route: complex (rule: core file `src/flask/app.py`)."*
- *"Route: simple (Jev, p=0.93): 1 file, 2 lines changed."*
- *"Route: complex (fallback: Jev unavailable)."*

| PR | Route |
|---|---|
| Docs only | simple (rule) |
| Docs only, but one contains an API key | complex (rule) |
| Lockfile only (dependency bump) | simple (rule); the review names the dependency update |
| A typo fixed in a comment in one `.py` file | Jev decides, which is likely simple |
| A one-line condition change in a helper | Jev decides, which is likely complex |
| 3 docs + 2 source (rate limiter, new module) + 1 test | complex (rule: new module) |
| 25 source files | complex (rule) |

### 3.4 Large PRs on the complex path
There's no separate lane. The complex path scales through the review plan:
- **The architecture analyst** gets the ranked file list, a deterministic **API-surface extraction** (added or removed top-level `def`, `class` and public names), and full diffs of the top-priority files that fit the budget.
- **The risk agent** gets `must_review` first in its system prompt, and pulls any other diff on demand.
- **Coverage check:** after the agent finishes, any `must_review` file it didn't read becomes a *"verify: not inspected"* focus area, and confidence is capped by coverage (§8).
- **The synthesizer's input stays bounded:** at most 15 findings, sorted by severity, plus counts of the rest.
- **Very large PRs:** above 40 source files or 6 modules, a deterministic focus area recommends splitting the PR.

### 3.5 Large single files
`read_file_diff(path, page)` returns the hunk headers plus about 400 lines per page. The agent can request further pages, so nothing is ever cut silently.

---

## 4. Context sources & limitations

There is no GitHub access, which the assignment requires, so the system **never sees the full repository**. This is an accepted constraint, and the design handles it openly rather than working around it.

**What the system knows:**
1. **The PR JSON:** file paths (structure), hunk headers (enclosing function or class), context lines, and the description (intent).
2. **The model's built-in knowledge** of widely used projects such as Flask. Prompts require every finding to cite diff evidence and to label general-knowledge claims as assumptions.
3. **`project_context.md`:** one hand-written page of the project's conventions, taken from public docs, plus the core path patterns. It's swappable per repo.

**What it cannot know:** code outside the diff, whether tests still pass, and project history.

**How the design handles it:**
- confidence is capped when a review depends on unseen code,
- uncertain points become "verify X" focus areas,
- the extension point is new tools (`read_repo_file`, `find_usages`, a code-search index), with no graph redesign,
- the README states these limits.

---

## 5. Models

| Node | Model | Effort | Why |
|---|---|---|---|
| `classify_pr` | **Jev** (TypeSafe decision model) | n/a | Returns a probability for one decision. It's fast (vendor-reported 70–500ms) and cheap. The calibrated probability is what makes a threshold and a fallback possible. |
| `architecture_analyst` | Claude Opus 5.5 (`claude-opus-5-5`) | medium | One focused judgment call |
| `risk_agent` | Claude Opus 5.5 | **high** | Multi-step tool use and the hardest reasoning |
| `synthesize` (simple path) | Claude Opus 5.5 | low | A small diff that needs a short review |
| `synthesize` (complex path) | Claude Opus 5.5 | medium | Merges findings that already exist |

One Claude model means one prompt cache and one set of behaviours to handle. Models, effort levels and the Jev threshold are set in `config.py` and can be overridden from the environment (`MODEL_MAIN`, `JEV_THRESHOLD`).

### Claude API behaviours that shape the code
1. **Thinking is always on,** and `effort` is the control. Every node sets its effort explicitly.
2. **No `temperature`/`top_p`** (they return a 400). Consistency comes from the deterministic nodes and strict schemas.
3. **Forced tool choice returns a 400,** so structured output must use **native JSON-schema structured outputs**: `with_structured_output(method="json_schema")`. This is also why the agent's final answer is a separate structured call (§7) rather than a forced "answer" tool. *Verify the exact path against the installed `langchain-anthropic` at implementation.*
4. **Refusals** (`stop_reason: "refusal"`) can be false positives on security-related diffs. Enable the server-side fallback (`fallbacks: "default"`). A final refusal is handled as a degraded stage.
5. **Prompt caching:** static content (instructions, project context) goes first, and PR content last. We set the `cache_control` breakpoints ourselves: one on the system prompt, and one on the latest message of each agent turn, so the growing history is cached incrementally. *Verify how `langchain-anthropic` expresses `cache_control` at implementation.*
6. **Thinking blocks in the tool loop:** with thinking on, each assistant turn must be sent back unchanged, thinking blocks included, or the API rejects the next request. Our loop appends the model's `AIMessage` as-is and never rewrites it.
7. **`max_tokens` of about 16K** per call, because thinking counts toward it.
8. **1M context:** even huge PRs fit. The budgets exist for **focus and cost**, not to fit the window.

### Jev: to verify in step 6 before writing any code
- the endpoint, authentication, request and response format, and pricing, all from the official docs,
- how a binary decision and its probability are expressed,
- whether the vendor's latency and accuracy claims hold on our inputs. These come from vendor and blog sources and haven't been independently verified.

**Rough cost:**
- the sample PR (complex path): about $0.25–0.40 per run,
- a simple-path PR: a few cents (one low-effort call),
- a Jev call: verify in step 6.

---

## 6. State

```python
class RouteDecision(BaseModel):
    route: Literal["simple", "complex"]
    source: Literal["rule", "jev", "fallback"]
    probability: float | None                      # P(simple), set only when Jev decided
    reason: str

class ReviewState(TypedDict):
    raw_pr: dict                                   # input
    pr: PRInput                                    # normalized, files classified + prioritized
    stats: PRStats                                 # counts, token estimate, filtered files, scan hits
    plan: ReviewPlan                               # ranked_files, must_review, budgets (set by parse_pr)
    route: RouteDecision                           # set by classify_pr
    architecture: ArchitectureFindings | None
    risk: RiskFindings | None
    files_inspected: list[str]                     # what the agent actually read
    draft: ReviewDraft | None
    validation_errors: list[str]
    revision_count: int
    degraded: Annotated[list[str], operator.add]   # stages that failed or fell back
    reasoning_trace: Annotated[list[str], operator.add]
    output: ReviewOutput | None
```

- **Graph interface:** input `{raw_pr}`, output `{output}`. Everything else is internal working memory.
- **Not in the main state:**
  - the agent's message history (it lives only in the subgraph's `AgentState` and is discarded after the node),
  - the tool context (the PR given to the tools, read-only),
  - config and secrets.

### The risk agent's own state (subgraph)
```python
class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # system, user, AI turns, tool results
    model_calls: int                                      # counted by the agent node
    tool_calls: int                                       # counted by the tools node
    files_inspected: Annotated[list[str], operator.add]   # recorded by the tools node on each read
    budget_exhausted: bool
    findings: RiskFindings | None                         # set by the respond node
```

The `risk_agent` node in the main graph is a thin wrapper:
1. it builds the initial messages and budgets from the main state,
2. it invokes the compiled subgraph,
3. it maps only `findings`, `files_inspected` and `budget_exhausted` back to the main state.

The two states stay independent.

---

## 7. The risk agent: a hand-built ReAct loop (`risk_agent`)

### Why not `create_agent`
`create_agent` hides the loop: how the agent stops, how limits apply, how tool errors are handled, and how the final structured answer is produced. All of that lives inside prebuilt middleware. A reviewer can't see those decisions, and we can't test them directly.

Instead, the loop is a **small LangGraph subgraph we write ourselves**, so every one of those decisions is explicit code.

```
START → agent ─┬─ has tool calls ──► tools ─┬─ budget left ──► agent   (loop)
               │                            └─ exhausted ────► respond
               └─ no tool calls ────────────────────────────► respond → END
```

### Nodes
| Node | What it does |
|---|---|
| `agent` | One call to `ChatAnthropic.bind_tools(TOOL_SCHEMAS)` (effort: high) over `messages`, then increment `model_calls`. The `AIMessage` is appended unchanged, thinking blocks included (§5). |
| `tools` | **Our own executor** (not the prebuilt `ToolNode`). For each tool call in the last `AIMessage`: validate the arguments, dispatch to the Python function with the PR context, record files read in `files_inspected`, and increment `tool_calls`. It returns **one `ToolMessage` per tool call**, which the API requires, even when the call fails or is over budget. |
| `respond` | **One structured call**, `with_structured_output(RiskFindings, method="json_schema")` over the conversation: *"Report only findings backed by evidence you saw."* This path is the same whether the agent finished on its own or ran out of budget, so the "limit hit" case needs no special fallback. |

### Routing (two conditional edges)
- **After `agent`:** go to `tools` if the last message has tool calls, otherwise go to `respond`.
- **After `tools`:** go to `respond` if `model_calls ≥ max_model_calls` or `tool_calls ≥ max_tool_calls`, otherwise go back to `agent`.
- **Backstop:** the subgraph is invoked with `recursion_limit = 2 × max_model_calls + 5`. It should never trigger. If it does, the node degrades (§9).

### Budgets, from the review plan (§3.2)
- `max_tool_calls = clamp(8 + 2 × code_files, 8, 30)`
- `max_model_calls = max_tool_calls // 2 + 2`. Claude can request several tools in one turn.
- When a turn requests more tools than the remaining budget allows, the extra calls get a *"budget exhausted"* `ToolMessage`, and the loop moves to `respond`.

### Tools
Each tool has two parts:
- **A Pydantic argument schema**, bound to the model with `bind_tools`.
- **A plain Python function** `fn(ctx, args) -> str`.

A registry maps `name → (schema, fn)`. The executor passes the read-only PR context **explicitly**, with no globals and no runtime injection.

| Tool | Returns |
|---|---|
| `list_changed_files()` | Path, status, +/-, category, priority, and a flag for filtered files |
| `read_file_diff(path, page=1)` | The hunk headers plus one page (~400 lines) of the diff, with "page X of N" |
| `search_diff(pattern)` | Regex hits across all diffs as `file:line: text` (at most 50 hits) |
| `read_project_context(topic)` | A section of `project_context.md` |

### Tool error handling (in the executor)
Each error becomes a `ToolMessage` with `status="error"` and a message the agent can act on. The loop never crashes.

| Error | Message back to the agent |
|---|---|
| Unknown tool name | The list of valid tools |
| Arguments fail schema validation | The validation errors |
| Unknown file path | The 3 closest paths (`difflib`) |
| Invalid regex | The regex error, plus a hint to escape the special characters |
| Page out of range | The valid page range |
| Any other exception | The exception type and message, with no stack trace |

Claude can request several tools in one turn. They're in-memory and take microseconds, so the executor runs them **in order**, and the results go back in that same order.

### System prompt
It's built in plain code by the wrapper node, from the PR overview and `must_review`. It holds:
- the role and the instructions (static, so they're cached),
- the risk categories and the evidence rule,
- `must_review` and the budget (*"you have N tool calls"*),
- the PR overview in a delimited data block (§9).

### Post-processing (in the wrapper node, deterministic)
- **Hallucination guards:**
  - findings that name a file not in the PR are dropped,
  - each `evidence` must appear in the diff (compared after normalizing whitespace). If it doesn't, the finding is marked *unverified*, its severity drops one level, and confidence is lowered.
- **Coverage check:** unread `must_review` files become "verify" focus areas (§3.4). `files_inspected` is recorded by the executor as it runs, so there's no parsing of the message history afterwards.

### What this gives us over `create_agent`
- **Every rule is visible in code we own:** stop conditions, budgets, error messages and the final answer.
- **Testable without an API key:** a scripted fake chat model that returns preset tool calls can exercise the budget stop, the error paths and coverage tracking.
- **One clear trace:** the `agent`, `tools` and `respond` steps show up as nested spans.

---

## 8. Synthesize, validate, finalize

### `synthesize`
- **Input:**
  - **simple path:** the PR overview plus the diffs, in priority order, within about 20K tokens,
  - **complex path:** the findings and stats only, never raw diffs.
- **Output:** `ReviewDraft`, containing `risk_level`, `risk_reasoning`, `architectural_impact`, `review_focus_areas` (`{file, concern}`, ordered, 1–6 items), `confidence` and `confidence_reasoning`.
- **Risk rubric:**
  - **high:** security, a breaking public API change, or a risky change on a core hot path without tests,
  - **medium:** a core-path change, new shared state, or a concurrency or deployment concern,
  - **low:** isolated changes, docs, tests, or refactors with no behaviour change.
- **Rules:**
  - the level is never below the strongest well-evidenced finding unless it's justified,
  - conflicts between the analysts must be addressed openly,
  - uncertainty becomes "verify X".

### `validate`
**Checks:**
1. Every focus area names a changed file.
2. The risk level is consistent with the findings (complex path), or with the deterministic signals (simple path).
3. Required fields are non-empty.

On failure it loops back to synthesize once, passing the errors and the previous draft.

### `finalize`
**`complexity_score` (1–10):**
`clamp(1 + size + spread + core + new_modules, 1, 10)`, rounded to 0.5, where:

| Term | Formula |
|---|---|
| `size` | `3·log10(1+L)/log10(1001)`, max 3. L = source and config lines, excluding tests, docs and filtered files. |
| `spread` | `2·(n−1)/9`, max 2. n = source files. |
| `core` | 1.5 per core file, max 3 |
| `new_modules` | 0.5 each, max 1 |

Example: the sample PR scores 5.5 and a docs typo scores 1.0. The weights live in config, and the README notes that the score measures structure, not subtlety.

**`confidence` (0–1):** start from the LLM's value. **Subtract the penalties**, then **apply the caps with `min()`**, then clamp to [0.1, 0.95].

| Signal | Effect |
|---|---|
| Coverage of `must_review` files the agent read | cap at `0.5 + 0.5·coverage` |
| Truncated diffs (budget or malformed) | cap at 0.7 |
| Analysts disagree (`impact_level` vs `max_severity`) | 1 level: −0.1; 2 levels: cap at 0.6 |
| Agent hit its limit | cap at 0.6 |
| Validation retry | −0.05; still failing: cap at 0.5 |
| Core files modified (callers unseen) | −0.05 each, max −0.1 |
| Unverified findings | −0.05 each, max −0.15 |
| Degraded stage: architecture / risk | cap at 0.5 / 0.4 |

Each adjustment is written to the trace.

**Assembly:**
- focus areas become `"path: concern"` strings, deduplicated, at most 6,
- the trace is numbered `Step N: ...` and includes the route decision,
- the result is validated as a `ReviewOutput`.

---

## 9. Edge cases & error handling

### Exit codes
| Code | Meaning |
|---|---|
| 0 | Full review |
| 2 | Invalid input, nothing written |
| 3 | **Degraded** review written |
| 1 | Fatal error, nothing produced |

### Input and parsing
| Case | Handling |
|---|---|
| File missing, unreadable or not UTF-8 | Exit 2 with the path and reason |
| Input over 20MB | Exit 2 |
| Invalid JSON | Exit 2 with line and column |
| Schema errors | Exit 2, listing **all** Pydantic errors with field paths |
| Missing optional fields (`pr_description`, `author`) | Default to empty and note it |
| Unknown extra fields | Ignored |
| Unknown `status` | Mapped to a known value (`removed` → `deleted`). Otherwise `modified`, with a warning. |
| Line counts don't match the diff | Trust the diff, recompute, warn |
| Duplicate paths | Merge and warn |
| Malformed diff (no `@@`) | Treat as raw text, flag it, route `complex`, cap confidence |
| `changed_files` empty | Exit 2 |
| Only mechanical files | Simple path, stated in the review |

### Routing failures
| Failure | Handling |
|---|---|
| No `JEV_API_KEY` | Rules only; the uncertain middle goes to `complex`. Noted in the trace. |
| Jev timeout (~3s), connection error or 5xx | 1 quick retry, then `complex` |
| Jev 4xx or an unexpected response shape | `complex` immediately, with a warning in the trace |
| `P(simple)` below the threshold | `complex` |

A routing fallback is a **safe default, not a degradation**. The review is still full, so the exit code stays 0.

### LLM failures: one retry layer per failure type
| Failure | Handled by |
|---|---|
| 429, 5xx, timeout, connection error | Anthropic SDK `max_retries=3` with exponential backoff |
| Output doesn't match the schema | Node `RetryPolicy(max_attempts=2)`, retrying only on validation errors |
| 400 / 401 / 403 | Fail fast |
| Refusal | Server-side fallback (§5), then degraded |

### Degradation when a node fails after retries
| Node | Result |
|---|---|
| `architecture_analyst` | `None`; synthesize works from risk findings only |
| `risk_agent` | `None`; synthesize works from architecture findings only |
| `synthesize` | **Deterministic fallback review:** risk level from the heuristics, focus areas = the top-priority files, confidence 0.2, exit 3 |

### Untrusted input
- **Prompt injection:**
  - PR text goes in delimited data blocks, and the system prompt says it's data, not instructions,
  - a deterministic scan for injection-like phrases forces `complex`, adds a focus area and lowers confidence,
  - `validate` checks the risk level against the findings, which is a second defence.
- **Secrets:** a regex scan for obvious credentials (`AKIA…`, private key headers, `api_key = "…"`) forces `complex` and a high-severity finding.

### Output
- Create the parent directory if missing.
- **Atomic write** (a temp file, then rename).
- If the path is unwritable, print the JSON to stdout and exit 1.

---

## 10. Optimizations
1. **Skip work:**
   - the noise filter,
   - the simple path skips both analysts,
   - Jev moves small, harmless code changes to the simple path,
   - no raw diffs go to the complex-path synthesizer.
2. **Parallelism:** the two analysts run at the same time.
3. **Effort per node** (§5): low on the simple path, high only for the risk agent.
4. **Cheap decisions first:** the rules are free, and Jev is called only for the uncertain middle.
5. **Prompt caching:** static-first prompt order, plus our own `cache_control` breakpoints in the agent loop, so each turn reuses the cached history.
6. **Budgets that scale with the PR,** within hard caps.
7. **Usage accounting:** tokens in/out and estimated cost go into the trace, which works without Langfuse.

---

## 11. Deliberately not built
- **Checkpointer / Store:** this is a one-shot CLI and the assignment forbids persistence. The README explains when we'd add them: multi-turn follow-ups, resuming, HITL via `interrupt()`, long-term memory of maintainer preferences.
- **A per-file pre-scan (`Send` map-reduce):** see the alternatives in §2.
- **Prebuilt agent abstractions** (`create_agent`, agent middleware, the prebuilt `ToolNode`): replaced by our own subgraph for full control (§7).
- **`ModelRetryMiddleware`-style retries in the loop:** the Anthropic SDK already retries transient errors (§9), and stacking retry layers multiplies the wait.
- **Summarization or context editing in the loop:** the agent's conversations are short and bounded by our budgets.
- **Vector DB / RAG:** the project context is one page. It would become justified with full-repo context (§4).
- **LLM response cache across runs:** that's persistence.
- **Streaming and the Batch API:** this is a batch CLI, and the Batch API's asynchronous latency is wrong for a CLI.
- **Eval datasets, Langfuse scores, more than one test.**

---

## 12. Langfuse (development only, not submitted)
`observability.py` returns a `CallbackHandler` only if the keys are set and the package is installed. The handler is passed in the graph config, giving one nested trace per run: the route decision, the analysts in parallel, the agent's tool calls and the validate loop. There are no scores or experiments.

---

## 13. File layout

```
analyze_pr.py                    # CLI: args, exit codes, atomic write
pr_input.json                    # mock PR (provided)
code_review_output.json          # generated deliverable
requirements.txt                 # uv export, without the dev/tracing group
README.md
docs/PLAN.md
src/agent_workflow/
  config.py          # models, effort per node, routing thresholds, Jev threshold, weights, budgets
  llm.py             # Claude factory: ChatAnthropic, effort, fallbacks, structured-output helper
  router.py          # classify_pr: hard rules, Jev client, fallback
  schemas.py         # Pydantic models (input, plan, route decision, findings, draft, output)
  state.py           # ReviewState + reducers
  prompts.py         # all prompts (static content first)
  preprocessing.py   # normalize, noise filter, classify, priority, review plan, scans, API surface
  scoring.py         # complexity, confidence, deterministic fallback review
  tools.py           # 4 tools: arg schemas + functions + registry, and the executor with error handling
  risk_agent.py      # ReAct subgraph: AgentState, agent/tools/respond nodes, routing, budgets, wrapper, guards
  nodes.py           # graph nodes
  graph.py           # build_graph(): routing, parallel analysts, cycle, RetryPolicy
  observability.py   # optional Langfuse handler (dev only)
  project_context.md # Flask conventions + core path patterns (swappable)
tests/test_pipeline.py           # one deterministic test: preprocessing + routing rules + scoring, no API key
```

---

## 14. Implementation steps (to do together)

Each step ends with something runnable, which we review before moving on.

- [x] **0. Housekeeping:** add `.env` to `.gitignore`.
- [ ] **1. Setup & schemas:** replace `langchain-openai` with `langchain-anthropic`, add `ANTHROPIC_API_KEY` (and an optional `JEV_API_KEY`) to `.env.example`, move langfuse into a dev group, add `pr_input.json`, write `schemas.py` and `state.py`. *Check:* the sample loads and validates, and bad inputs give clear errors.
- [ ] **2. Deterministic layer:** `preprocessing.py` (including the review plan), the rules part of `router.py`, `scoring.py`, `validate`, and the test. *Check:* run it on the sample, on a docs-only PR, on a one-line comment fix (it should land in the uncertain middle) and on a synthetic 60-file PR.
- [ ] **3. Graph skeleton:** `graph.py` with every LLM node stubbed and both paths wired up. *Check:* it compiles, prints Mermaid, and both paths run end to end on stubs.
- [ ] **4. LLM foundation:** `llm.py` (verify structured output, effort, fallbacks and caching against the installed `langchain-anthropic`), then `architecture_analyst` and `synthesize` (both modes). *Check:* the first real run on the sample.
- [ ] **5. ReAct agent (hand-built):**
  - `tools.py` (the registry and the executor),
  - `project_context.md`,
  - `risk_agent.py` (the subgraph, budgets and wrapper),
  - the hallucination guards and the coverage check.

  *Check:*
  - the subgraph prints its own Mermaid,
  - on the sample PR, the tool calls make sense,
  - a forced tiny budget ends the loop cleanly in `respond`,
  - a bad path or regex comes back as a recoverable error.
- [ ] **6. Jev router:** read the official Jev docs first, then add the client, the threshold and every fallback in §9. *Check:* the uncertain-middle PRs are routed with and without `JEV_API_KEY`, a simulated timeout falls back to `complex`, and the trace shows the decision source.
- [ ] **7. Degradation & CLI:** fallbacks per node, exit codes, the atomic write. *Check:* simulated failures produce degraded output with exit code 3.
- [ ] **8. Langfuse tracing (dev only).** *Check:* a nested trace appears in your UI.
- [ ] **9. Deliverables:** `code_review_output.json`, `requirements.txt` and the README (design, trade-offs, the Jev decision and fallback, AI-tool usage, what was new to you).

## 15. Before we start
- **Step 1:** put the full `pr_input.json` in the repo root, and add `ANTHROPIC_API_KEY` to `.env`.
- **Step 6 (optional):** add `JEV_API_KEY` to `.env`. Without it, the router runs on rules plus the `complex` default.
