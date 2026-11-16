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
6. **Explicit configuration, no hidden behaviour.** Framework components are used where they fit, but every limit, error policy and stop condition is chosen and configured in our code, and each one is tested (§7). No framework default goes unreviewed.

### Stack
| Package | Role | Submitted? |
|---|---|---|
| langgraph | Orchestration: the main graph, conditional routing, parallel fan-out/fan-in, bounded cycles, RetryPolicy. The risk agent is itself a compiled LangGraph graph. | Yes |
| langchain | The risk agent (§7): `create_agent`, its middleware (tool-call and model-call limits, tool errors, refusals), `ProviderStrategy` structured output, and tools defined with `@tool` + `ToolRuntime` | Yes |
| langchain-core + langchain-anthropic | `ChatAnthropic`, message types, `with_structured_output` for the single-call nodes, and `AnthropicPromptCachingMiddleware` for the agent | Yes |
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
| `parse_pr` | Deterministic | Validate and normalize the input, filter mechanical files, classify and prioritize files (one `FileClassification` per file, §6), scan for secrets and injection attempts, build the review plan (must-review files and budgets) |
| `classify_pr` | Rules + decision model (Jev) + fallback | Choose `simple` or `complex` (§3.3). A conditional edge reads its decision. |
| `architecture_analyst` | 1 LLM call | Architectural impact. It needs no exploration, so it isn't an agent. |
| `risk_agent` | **ReAct agent**, built with `create_agent` and explicit middleware (§7) | Concrete risks backed by evidence, investigated through tools |
| `synthesize` | 1 LLM call | Simple path: review the small diff directly. Complex path: merge the analysts' findings. On a retry it also gets the validator's feedback. |
| `validate` | Deterministic + conditional edge | Check that the draft is grounded and consistent, with at most 1 retry (a real cycle) |
| `finalize` | Deterministic | Compute the complexity score and calibrate confidence, then assemble the output |

### LangGraph features and why each one is used
| Feature | Where | Why |
|---|---|---|
| Conditional edges | After `classify_pr`, after `validate` | The path depends on the PR and on the draft's quality |
| Parallel fan-out / fan-in | The two analysts start together and join at `synthesize` | They're independent, so running them together halves the latency. LangGraph waits for both before `synthesize` runs. |
| Bounded cycles | `synthesize ⇄ validate`, and `model ⇄ tools` inside the risk agent | Self-correction (at most 1 retry), and the ReAct loop (bounded by the limit middleware, §7) |
| Subgraph | `risk_agent` | `create_agent` returns a compiled LangGraph graph with its own state. The wrapper node invokes it, so it's testable alone and prints its own Mermaid. |
| Reducers (`operator.add`) | `reasoning_trace`, `degraded` | Both parallel branches write to them safely |
| `RetryPolicy` | LLM nodes | Retry when the output doesn't match the schema |
| Input/output schemas | Graph | The caller sees `{raw_pr}` → `{output}`, and everything else stays internal |

**`Send` is not used.** `Send` is for a *dynamic* number of tasks decided at runtime, such as one per file. Our fan-out always has exactly two fixed branches, so static edges are the right tool.

### Alternatives considered
- **A single ReAct agent.** It's simpler, but it mixes responsibilities, has no guardrails, and its output is harder to validate.
- **Three lanes with a per-file triage pre-scan via `Send`** (an earlier version of this plan). It cut costs only on 100-file PRs, but added two nodes, a clustering algorithm and a second model. It was removed: the risk agent already pulls diffs on demand, and the 1M-token context fits large PRs.
- **A hand-built ReAct subgraph for the risk agent** (an earlier version of this plan). It made every rule our own code, but it reimplemented about 150 lines that `create_agent` already provides and tests: tool dispatch with one `ToolMessage` per call, unknown-tool and invalid-argument errors, call limits, and the structured final answer. With `create_agent`, each of those rules is still chosen explicitly through named middleware and covered by our tests (§7). The trade-off is that the loop's mechanics live in library code, so we pin the version and test the behaviour we rely on.
- **Claude as the router.** Claude doesn't expose token probabilities, so we'd get a self-reported confidence rather than a calibrated one. A decision model returns a real probability.
- **Jev for every routing decision.** Rules are free, exact and deterministic for the clear cases, so Jev is used only where the rules can't decide.

---

## 3. Adapting to the PR

### 3.1 Noise filter (deterministic, runs first)
| Category | Detection |
|---|---|
| Lockfiles / generated / vendored | `*.lock`, `package-lock.json`, `*.min.js`, `*_pb2.py`, `generated/`, `vendor/` |
| Pure rename | `renamed` with no `@@` hunks (checked before binary/empty, because a pure rename usually has an empty diff) |
| Binary / empty | An empty diff, or a git binary marker (`Binary files … differ`, `GIT binary patch`). An empty diff **with reported changes** is not mechanical: its patch is missing (§9). |
| Formatting only | In each change block (a run of removed and added lines between context lines), the removed and added lines are the same, **in order**, after stripping **trailing whitespace only**. Leading indentation and inner whitespace must match exactly: indentation is syntax in Python, and whitespace inside a string literal changes behaviour. So a dedent, a reordering, a line moved past a context line, or `"  "` → `" "` is a code change. |
| Import only | Python files (`.py`, `.pyi`) only: every non-blank changed line is a complete `import …` / `from … import …` statement with nothing after it (no `;`). Other languages are never import-only, and neither is `__init__.py`/`__init__.pyi`: its imports are the package's public API. |

Rows are checked top to bottom, and the first match is the `filter_reason`. An **added or deleted** file can only match the lockfile/generated/vendored row or a binary marker: adding or removing a module is a structural change even if it holds only imports or nothing at all (e.g. an empty `__init__.py`). A non-empty diff with no `@@` hunk that matches none of these rows is **malformed**, not mechanical (§9).

Filtered files are counted in the stats ("12 mechanical files skipped") and listed by the agent's tools, but they are never sent to an analyst.

**Only mechanical files are filtered.** Docs and tests have lower priority but are still included as context. For example, the architecture analyst can check that the docs match the code.

**File categories** are checked in this order, and the first match wins:
1. **mechanical** (the table above)
2. **test:** `tests/`, `test_*.py`, `*_test.py`, `conftest.py`
3. **docs:** `docs/`, `*.md`, `*.rst`, `*.txt`, `CHANGES`, `LICENSE`. **Exception:** `requirements*.txt` is checked just before this rule and is **config**, because a dependency manifest changes what gets installed.
4. **config:** `pyproject.toml`, `*.cfg`, `*.ini`, `*.yaml`, `*.yml`, `.github/workflows/`, and `requirements*.txt` (see above)
5. **source:** everything else, **including unknown file types**. A doubtful file is treated as code. A source file whose extension isn't in the known-code list in `config.py` is also flagged `is_unknown_type`, which routes `complex` (§3.3).

### 3.2 Priority and the review plan (in `parse_pr`)
`priority = 3·is_core + 2·is_source + 1.5·is_new_module + 1·sensitive_path + log10(1+lines_changed) − 1·is_test − 2·is_docs`

- `is_core` comes from the core path patterns (globs) in the `## Core paths` section of `project_context.md`. A project without that section has no core files.
- `sensitive_path` uses generic patterns, matched as case-insensitive substrings of the path: `auth`, `security`, `session`, `crypto`, `config`, `middleware`, `migrations`. It applies only to code files (source, config and test), so a docs-only PR such as `docs/config.rst` still routes `simple`. Over-matching on code files is accepted, because it only makes routing more thorough.
- `is_new_module` means an added source file.
- `lines_changed` is the recomputed `lines_added + lines_removed`.
- Priority is computed for every file, filtered ones included, and stored in `FileClassification`.
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
| Any core file, sensitive path, new module or deleted source file (non-filtered files only: a formatting- or import-only change to a core file has nothing to review) | `complex` |
| More than 3 code files, or more than 50 changed code lines | `complex` |
| Unknown file type or malformed diff | `complex` |

Code files are source, config and test files. The thresholds live in `config.py`. A dependency-only PR (`requirements*.txt` is config) has no special rule: it lands in the uncertain middle. A lockfile-only PR is mechanical and routes `simple`.

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
- **Coverage check:** after the agent finishes, every `must_review` file it didn't read is flagged in the focus areas, and confidence is capped by coverage (§8). `finalize` does this, since the draft exists only after `synthesize`. A focus area the draft already has for that file gets the suffix *"(verify: not inspected by the risk agent)"*. The unread files the draft doesn't name are listed together in **one** PR-wide *"verify: … not inspected"* item, which has a reserved slot, so the limit of 6 can only cut draft items, never an unread file.
- **The synthesizer's input stays bounded:** at most 15 findings, sorted by severity, plus counts of the rest.
- **Very large PRs:** above 40 source files **and** above 6 modules (distinct parent folders), a deterministic focus area recommends splitting the PR. "And", because a small PR spread over many folders (e.g. a rename across packages) is usually fine as one PR.

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
3. **Forced tool choice returns a 400,** so structured output must use **native JSON-schema structured outputs**:
   - the single-call nodes use `with_structured_output(method="json_schema")`,
   - the agent uses `response_format=ProviderStrategy(RiskFindings)`. **`ToolStrategy` is not used**, because it binds the tools with `tool_choice="any"` (checked in the `langchain` 1.4.9 source). `langchain-anthropic` 1.7.10 maps `ProviderStrategy` to Anthropic's `output_config.format`.
   - *Verify at implementation:* that the native format works alongside tool calls and thinking on a real run.
4. **Refusals** (`stop_reason: "refusal"`) can be false positives on security-related diffs. Enable the server-side fallback (`fallbacks: "default"`). A final refusal is handled as a degraded stage. In the agent, `ModelRefusalMiddleware` turns it into an error, so it can't be mistaken for a final answer.
5. **Prompt caching:** static content (instructions, project context) goes first, and PR content last. In the agent, `AnthropicPromptCachingMiddleware` tags the system prompt and the tool definitions and caches the message tail, so the growing history is cached on every turn. The single-call nodes put a `cache_control` breakpoint on the system prompt. *Verify the cache hits in the usage metadata at implementation.*
6. **Thinking blocks in the tool loop:** with thinking on, each assistant turn must be sent back unchanged, thinking blocks included, or the API rejects the next request. `create_agent` appends the model's `AIMessage` to the state as-is, and none of our middleware rewrites messages. *Verify on a multi-turn run in Phase 5.*
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

class FileClassification(BaseModel):              # system-generated, one per file; never read from the input
    path: str
    status: Literal["added", "modified", "deleted", "renamed"]   # mapped from the raw status (§9)
    status_was_unknown: bool
    lines_added: int                               # recomputed from the diff
    lines_removed: int
    category: Literal["mechanical", "test", "docs", "config", "source"]
    filter_reason: str | None                      # mechanical files only
    is_core: bool
    is_sensitive: bool
    is_new_module: bool
    is_unknown_type: bool
    is_malformed: bool
    priority: float

class ReviewState(TypedDict):
    raw_pr: dict                                   # input
    pr: PRInput                                    # validated input data only (duplicate paths merged)
    classifications: dict[str, FileClassification] # path → derived facts (set by parse_pr)
    stats: PRStats                                 # counts (incl. filtered), token estimate, scan hits
    plan: ReviewPlan                               # ranked_files, must_review, budgets (set by parse_pr)
    route: RouteDecision                           # set by classify_pr
    architecture: ArchitectureFindings | None
    risk: RiskFindings | None
    files_inspected: list[str]                     # what the agent actually read
    risk_budget_exhausted: bool                    # the agent ran out of its tool/model-call budget (§7)
    draft: ReviewDraft | None
    validation_errors: list[str]
    revision_count: int
    degraded: Annotated[list[str], operator.add]   # stages that failed or fell back
    reasoning_trace: Annotated[list[str], operator.add]
    output: ReviewOutput | None
```

- **Graph interface:** input `{raw_pr}`, output `{output}`. Everything else is internal working memory.
- **Raw data and derived data are kept apart.** `ChangedFile` and `PRInput` hold only fields from the PR JSON. Unknown keys are dropped, so the input can't supply a category, priority or flag. Everything the system derives lives in `classifications`. When a path repeats, `parse_pr` merges the entries into one `ChangedFile`: the diffs are joined in order, and the first entry's status is kept. The untouched original stays in `raw_pr`.
- **Not in the main state:**
  - the agent's message history (it lives only in the agent's own state and is discarded after the node),
  - the tool context (the PR given to the tools, read-only),
  - config and secrets.

### The risk agent's own state
`create_agent`'s `AgentState` already holds `messages` and, with `response_format`, `structured_response`. The limit middleware keeps its call counters in private state. We extend it with one field:

```python
class RiskAgentState(AgentState):
    files_inspected: Annotated[list[str], operator.add]   # recorded by read_file_diff via Command(update=...)
```

The read-only tool context is a separate `context_schema` and is never part of the state:

```python
@dataclass(frozen=True)
class RiskAgentContext:
    pr: PRInput
    classifications: dict[str, FileClassification]
    project_context: str
```

The `risk_agent` node in the main graph is a thin wrapper:
1. it builds the system prompt, the budgets and the agent from the main state,
2. it invokes the agent with `context=RiskAgentContext(...)`,
3. it maps only the findings, `files_inspected` and `budget_exhausted` (as `risk_budget_exhausted`) back to the main state.

The two states stay independent.

---

## 7. The risk agent: `create_agent` with explicit middleware (`risk_agent`)

### Why `create_agent`
The risk agent is a standard tool-calling loop. LangChain's `create_agent` provides that loop as a compiled LangGraph graph, along with tool dispatch (the prebuilt `ToolNode`), limits, error handling and a structured final answer, each as a separate, named middleware. We use it instead of rebuilding those parts (§2, alternatives).

What stays under our control: every behaviour the loop has is one we chose and configured below. We take no defaults we haven't reviewed. Retries, summarization and context editing are not enabled (§11), and each rule we rely on has a test.

```
START → model ─┬─ has tool calls ──► tools ──► model   (loop, bounded by the limit middleware)
               └─ final answer ───► END       (structured_response: RiskFindings)
```

### Configuration
| Concern | Provided by | Our configuration |
|---|---|---|
| Model | `ChatAnthropic` from `llm.py` | Opus 5.5, effort high, `max_tokens` ~16K, server-side refusal fallback (§5) |
| System prompt | `system_prompt=` | Built in plain code by the wrapper (see below) |
| Tools | `@tool` with a Pydantic `args_schema` | 4 read-only tools (below) |
| PR context for the tools | `context_schema=RiskAgentContext`, read through `ToolRuntime` | Passed at invoke time and hidden from the model's tool schema. No globals. |
| Files read | `state_schema=RiskAgentState` | `read_file_diff` returns `Command(update={"files_inspected": [path], "messages": [...]})` |
| Tool budget | `ToolCallLimitMiddleware(run_limit=max_tool_calls, exit_behavior="continue")` | Over-budget calls get an error `ToolMessage` ("Tool call limit exceeded…") and the model goes on to answer |
| Model-call backstop | `ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end")` | Ends the run only if the model keeps requesting blocked tools after its tool budget is spent |
| Tool errors | `ToolNode` built-ins + `ToolErrorMiddleware(on_error=...)` | See "Tool error handling" |
| Final answer | `response_format=ProviderStrategy(RiskFindings)` | Native JSON schema. Not `ToolStrategy`, which forces `tool_choice` (§5). |
| Refusals | `ModelRefusalMiddleware` | A refusal raises an error, and the wrapper degrades the node (§9) |
| Prompt caching | `AnthropicPromptCachingMiddleware` (langchain-anthropic) | Caches the system prompt, tool definitions and message tail (§5) |

The agent is created once per run, inside the wrapper, because its budgets depend on the PR.

### Budgets, from the review plan (§3.2)
- `max_tool_calls = clamp(8 + 2 × code_files, 8, 30)`
- `max_model_calls = max_tool_calls + 2`. Every non-final turn uses at least one tool call, so the tool budget is the real limit on cost. The model-call limit is only a backstop: one call to answer after the last tool result, plus one call of slack for a turn whose tools were blocked. A tighter value would end normal one-tool-per-turn runs before the tool budget is spent, with no answer.
- **Normal stop:** the model answers without tool calls, and `structured_response` is set.
- **Tool budget used up:** further calls are blocked with an error `ToolMessage`, and the model answers with what it has.
- **Model-call limit reached:** the run ends without a `structured_response`. The wrapper then makes **one structured call**, `with_structured_output(RiskFindings, method="json_schema")`, over the conversation: *"Report only findings backed by evidence you saw."* Every path therefore ends with findings or a degraded node.
- **`budget_exhausted`** is true if the agent requested more tool calls than its budget (counted from the AI messages' `tool_calls`), or if the model-call limit ended the run. It caps confidence (§8).
- **Backstop:** the agent is invoked with a `recursion_limit` set above what the two limits allow. With this middleware, each model turn takes several graph steps, so the exact value is computed and tested in Phase 5. It should never trigger. If it does, the node degrades (§9).

### Tools
Each tool is a `@tool` function with a Pydantic `args_schema` and a `runtime: ToolRuntime[RiskAgentContext]` parameter. The runtime is injected and isn't shown to the model. Tools are pure reads over the PR context.

| Tool | Returns |
|---|---|
| `list_changed_files()` | Path, status, +/-, category, priority, and a flag for filtered files (from `classifications`) |
| `read_file_diff(path, page=1)` | The hunk headers plus one page (~400 lines) of the diff, with "page X of N" |
| `search_diff(pattern)` | Regex hits across all diffs as `file:line: text` (at most 50 hits) |
| `read_project_context(topic)` | A section of `project_context.md` |

Claude can request several tools in one turn. `ToolNode` may run them concurrently. That's safe, because the tools only read, and `files_inspected` uses an `operator.add` reducer. Results are matched to their calls by `tool_call_id`.

### Tool error handling
Each error becomes a `ToolMessage` with `status="error"` and a message the agent can act on. The loop never crashes.

| Error | Handled by | Message back to the agent |
|---|---|---|
| Unknown tool name | `ToolNode` (built in) | The list of valid tools |
| Arguments fail schema validation | `ToolNode` (built in) | The validation errors |
| Unknown file path | Our tool raises `ToolInputError` → `ToolErrorMiddleware` | The 3 closest paths (`difflib`) |
| Invalid regex | `ToolInputError` → `ToolErrorMiddleware` | The regex error, plus a hint to escape the special characters |
| Page out of range | `ToolInputError` → `ToolErrorMiddleware` | The valid page range |
| Over the tool budget | `ToolCallLimitMiddleware` | "Tool call limit exceeded. Do not make additional tool calls." |
| Any other exception | `ToolErrorMiddleware` | The exception type and message, with no stack trace |

### System prompt
It's built in plain code by the wrapper node, from the PR overview and `must_review`. It holds:
- the role and the instructions (static, so they're cached),
- the risk categories and the evidence rule,
- `must_review` and the budget (*"you have N tool calls"*),
- the PR overview in a delimited data block (§9).

### Post-processing (in the wrapper node, deterministic)
- **Hallucination guards:**
  - findings that name a file not in the PR are dropped,
  - each `evidence` must appear in the diff (compared after normalizing whitespace). If it doesn't, the finding is marked *unverified*, its severity drops one level, and confidence is lowered. The guards set `verified` on every finding. The field is hidden from the schema the model fills (`SkipJsonSchema`), and any `verified` in the input is dropped, so the model can't claim it. A guard sets it with `model_copy(update={"verified": True})`.
- **Coverage check:** the wrapper only records `files_inspected`, which comes from the agent's state, where `read_file_diff` records each read as it runs. `finalize` turns the unread `must_review` files into focus areas (§3.4), because the draft doesn't exist yet when the wrapper runs.

### Testing without an API key
`create_agent` accepts any `BaseChatModel`. A scripted fake chat model returns preset tool calls and a final JSON answer, which exercises the budget stop, the model-call backstop and its recovery call, the error paths and coverage tracking. The fake must accept `bind_tools(..., response_format=...)`. Build it in Phase 5.

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
2. The risk level is consistent:
   - complex path: it isn't below the highest severity among the *verified* risk findings,
   - both paths: a secret scan hit requires `high` (§9), except a hit in a test file, where fixtures commonly use fake keys. That hit still routes `complex` and is shown to the model, which judges it.
3. Required fields are non-empty.

On failure it loops back to synthesize once, passing the errors and the previous draft.

**Still failing after the retry:** `finalize` repairs only what rules can fix, so the output never breaks checks 1–2:
- the risk level is raised to the minimum the checks require: the highest verified risk finding (complex path), and `high` for a secret hit outside test files. A secret hit in a test file alone still doesn't force `high` (above). The reasoning then starts with a `[Repaired: …]` note, so it doesn't argue for the old level,
- focus areas that name a file not in the PR are dropped. If none is left, the rule-based focus areas (`must_review` first, as in the fallback review, §9) take their place, so a review always has focus areas.

Each repair is written to the trace (`Repair: …`), and confidence is capped at 0.5 (below). Empty fields aren't repaired. The model's other content is kept: switching to the fallback review (§9) would throw away a whole review over one bad path.

### `finalize`
**`complexity_score` (1–10):**
`clamp(1 + size + spread + core + new_modules, 1, 10)`, rounded to 0.5, where:

| Term | Formula |
|---|---|
| `size` | `3·log10(1+L)/log10(1001)`, max 3. L = source and config lines, excluding tests, docs and filtered files. |
| `spread` | `2·(n−1)/9`, max 2. n = source files. |
| `core` | 1.5 per core file, max 3 |
| `new_modules` | 0.5 each, max 1 |

Each term is clamped to `[0, max]`, and rounding is half-up. Example: the sample PR scores 5.5 and a docs typo scores 1.0. The weights live in config, and the README notes that the score measures structure, not subtlety.

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
| Unknown `status` | `ChangedFile` keeps the raw value. `parse_pr` maps it into `FileClassification.status` (`removed` → `deleted`), and anything unknown becomes `modified`, with a warning. |
| Line counts don't match the diff | Trust the diff: the recomputed counts go in `FileClassification`, with a warning |
| Duplicate paths | Merge and warn. Paths are compared after normalising (`./src/a.py` = `src/a.py`). |
| Missing or `null` diff | Treated as an empty diff |
| No `@@` hunk but reported changes (patch missing: an empty diff, e.g. a file too large to diff, or a rename whose content changes were left out) | Not filtered: flag it `is_malformed`, keep the reported counts, route `complex`, cap confidence. Lockfiles and generated/vendored files stay filtered. |
| Malformed diff (no `@@`, not mechanical, §3.1) | Treat as raw text, flag it (`is_malformed`), keep the reported line counts (the diff can't be counted), route `complex`, cap confidence |
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
| Refusal | Server-side fallback (§5), then degraded. In the agent, `ModelRefusalMiddleware` raises the error. |

### Degradation when a node fails after retries
| Node | Result |
|---|---|
| `architecture_analyst` | `None`; synthesize works from risk findings only |
| `risk_agent` | `None`; synthesize works from architecture findings only |
| `synthesize` | **Deterministic fallback review:** risk level `high` on a secret scan hit, `medium` on any core, sensitive, new-module or deleted source file or an injection hit, otherwise `low`. A secret hit in a test file counts as `medium`. Focus areas = `must_review`, then the other top-priority files (up to 6), confidence 0.2, exit 3. |

### Untrusted input
- **Prompt injection:**
  - PR text goes in delimited data blocks, and the system prompt says it's data, not instructions,
  - a deterministic scan for injection-like phrases forces `complex`, adds a focus area and lowers confidence,
  - `validate` checks the risk level against the findings, which is a second defence.
- **Secrets:** a regex scan for obvious credentials (`AKIA…`, private key headers, `api_key = "…"` and annotated forms, hard-coded `os.environ.get(…, "…")` defaults) over added lines, the title and the description forces `complex` and a high-severity finding. camelCase names that end in a keyword (`clientSecret`, `stripeApiKey`, `accessToken`) count too. Names like `tokenizer`, `token_url` or `isSecretValid` don't: only key-like names do.

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
5. **Prompt caching:** static-first prompt order, plus `AnthropicPromptCachingMiddleware` in the agent loop, so each turn reuses the cached history.
6. **Budgets that scale with the PR,** within hard caps.
7. **Usage accounting:** tokens in/out and estimated cost go into the trace, which works without Langfuse.

---

## 11. Deliberately not built
- **Checkpointer / Store:** this is a one-shot CLI and the assignment forbids persistence. The README explains when we'd add them: multi-turn follow-ups, resuming, HITL via `interrupt()`, long-term memory of maintainer preferences.
- **A per-file pre-scan (`Send` map-reduce):** see the alternatives in §2.
- **Retry middleware in the agent** (`ModelRetryMiddleware`, `ToolRetryMiddleware`): the Anthropic SDK already retries transient errors (§9), the tools are in-memory reads, and stacking retry layers multiplies the wait.
- **Summarization or context editing in the agent** (`SummarizationMiddleware`, `ContextEditingMiddleware`): the agent's conversations are short and bounded by our budgets.
- **Other agent middleware** (`ModelFallbackMiddleware`, human-in-the-loop, PII, to-do list, LLM tool selection, tool emulation): none of them solves a problem this agent has. Refusals use the server-side fallback instead (§5).
- **`create_agent` for the other LLM nodes:** `architecture_analyst` and `synthesize` have no tools, so each is one structured call, not an agent.
- **Vector DB / RAG:** the project context is one page. It would become justified with full-repo context (§4).
- **LLM response cache across runs:** that's persistence.
- **Streaming and the Batch API:** this is a batch CLI, and the Batch API's asynchronous latency is wrong for a CLI.
- **Eval datasets, Langfuse scores, more than one test module.**

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
  schemas.py         # Pydantic models (raw input, file classification, stats, plan, route decision, findings, draft, output)
  state.py           # ReviewState + reducers
  prompts.py         # all prompts (static content first)
  preprocessing.py   # normalize, noise filter, classify, priority, review plan, scans, API surface
  scoring.py         # deterministic post-LLM logic: complexity, confidence, draft checks (validate), fallback review
  tools.py           # 4 @tool functions with arg schemas, ToolRuntime context, ToolInputError + on_error handler
  risk_agent.py      # create_agent config: RiskAgentState, RiskAgentContext, middleware, budgets, wrapper, recovery call, guards
  nodes.py           # graph nodes
  graph.py           # build_graph(): routing, parallel analysts, cycle, RetryPolicy
  observability.py   # optional Langfuse handler (dev only)
  project_context.md # Flask conventions + core path patterns (swappable)
tests/test_pipeline.py           # one test module: the deterministic layer + the graph's two paths (LLM nodes stubbed), no API key
```

---

## 14. Implementation steps (to do together)

Implementation phases, their checks and progress are tracked in [`docs/phases.md`](phases.md).

## 15. Before we start
- **Step 1:** put the full `pr_input.json` in the repo root, and add `ANTHROPIC_API_KEY` to `.env`.
- **Step 6 (optional):** add `JEV_API_KEY` to `.env`. Without it, the router runs on rules plus the `complex` default.
