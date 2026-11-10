# Implementation Phases

This file sets the build order, the check for each phase, and progress.
`docs/PLAN.md` is the source of truth for the design. This file adds no scope.

**How we work:** one phase at a time. Each phase ends with something runnable that we review before moving on.
A phase is broken into sub-tasks before we start it, and the sub-tasks are reviewed first.

## Overview

| Phase | Goal | Done when | Status |
|---|---|---|---|
| 0 | Housekeeping | `.env` is gitignored | ✅ Done |
| 1 | Setup & schemas | The sample loads and validates, and bad inputs give clear errors | 🔄 In progress |
| 2 | Deterministic layer | Preprocessing, routing rules and scoring give correct results on the sample and synthetic PRs | — |
| 3 | Graph skeleton | The graph compiles, prints Mermaid, and both paths run on stubs | — |
| 4 | LLM foundation | First real run on the sample | — |
| 5 | ReAct risk agent (`create_agent`) | The agent loop works, stops on budget, and recovers from tool errors | — |
| 6 | Jev router | The uncertain middle is routed with and without `JEV_API_KEY` | — |
| 7 | Degradation & CLI | Simulated failures produce degraded output with exit code 3 | — |
| 8 | Langfuse tracing (dev only) | A nested trace appears in the Langfuse UI | — |
| 9 | Deliverables | `code_review_output.json`, `requirements.txt` and the README are done | — |

---

## Phase 0: Housekeeping ✅
- [x] Add `.env` to `.gitignore`.

---

## Phase 1: Setup & schemas

**Goal:** the project has the right dependencies, and the input, internal and output data models exist and validate.
**Files:** `pyproject.toml`, `uv.lock`, `.env.example`, `pr_input.json`, `src/agent_workflow/schemas.py`, `src/agent_workflow/state.py`.
**Design references:** PLAN.md §1 (stack, output fields), §6 (state), §8 (draft and output), §9 (input handling).

### Sub-tasks

- [x] **1.1 Prerequisites (you)**
  - Put the full `pr_input.json` in the repo root.
  - Add `ANTHROPIC_API_KEY` to `.env`.
  - *Blocks 1.4.*

- [x] **1.2 Dependencies (with `uv`)**
  - Remove `langchain-openai`, and add `langchain-anthropic` and `langchain-core`.
  - Keep `langchain` as a direct dependency, for `create_agent` and its middleware (PLAN.md §1, §7). *Decision changed:* it was first removed and then restored, after the risk agent was redesigned around `create_agent`.
  - Add `pydantic` as a direct dependency, since `schemas.py` imports it.
  - Move `langfuse` into a separate dev dependency group.
  - *Done when:* `uv sync` succeeds, `uv run python -c "import langchain, langchain_anthropic, langgraph"` works, and the planned agent configuration from §7 (`create_agent` + `ProviderStrategy` + the five middleware + `ToolRuntime` context + a custom state schema) compiles against the installed versions without an API call.

- [x] **1.3 `.env.example`**
  - Replace `OPENAI_API_KEY` with `ANTHROPIC_API_KEY`.
  - Add an optional `JEV_API_KEY`.
  - Mark the Langfuse keys as optional and dev-only.
  - *Done when:* the file holds no real values and lists every key the app reads.

- [x] **1.4 Inspect `pr_input.json`**
  - Write down its exact field names and types before writing the input schema.
  - The schema must fit any PR in this format, not just the sample.
  - *Result:* top level: `pr_title`, `pr_description`, `author`, `base_commit`, `head_commit` (str); `files_changed`, `total_additions`, `total_deletions` (int); `changed_files` (list). Each file: `path`, `status`, `diff` (str); `lines_added`, `lines_removed` (int). The sample's line counts match its diffs (+62, +53, +36). Commits and totals are optional in the schema, because preprocessing recomputes them.

- [x] **1.5 `src/agent_workflow/schemas.py`**
  - **Input (raw PR data only):** `ChangedFile` and `PRInput`, with the schema-level rules from §9:
    - missing or null `pr_description` and `author` default to empty,
    - unknown extra fields are ignored, which includes any classification-like keys,
    - `status` is kept as the raw non-empty string,
    - `changed_files` must not be empty.
  - **Internal (system-generated):** `FileClassification` (§6), which is never validated from the input. It holds the mapped `status`, the recomputed line counts, the category, the flags and the priority. Also `ScanHit`, `PRStats` (PR-level aggregates only), `ReviewPlan`, `RouteDecision`, `Finding`, `ArchitectureFindings`, `RiskFindings`, `FocusArea` and `ReviewDraft`.
  - **Output:** `ReviewOutput`, with exactly the 7 fields from §1.
  - Findings models stay minimal, using only the fields PLAN.md names (file, evidence, severity, `impact_level`, `max_severity`). They can be refined in Phases 4–5.
  - *Not here:* mapping the status, recomputing line counts, merging duplicate paths and detecting malformed diffs. That's preprocessing in Phase 2, which fills `FileClassification`.

- [x] **1.6 `src/agent_workflow/state.py`**
  - `ReviewState`, exactly as in §6, including `classifications: dict[str, FileClassification]`, with `operator.add` reducers on `degraded` and `reasoning_trace`.
  - The graph's input schema (`{raw_pr}`) and output schema (`{output}`).
  - *Not here:* `RiskAgentState` and `RiskAgentContext`, which live in `risk_agent.py` (Phase 5).

- [x] **1.7 Phase check**
  - The sample `pr_input.json` validates into `PRInput`.
  - Bad inputs give clear Pydantic errors that list every field path: missing `changed_files`, empty `changed_files`, wrong types, a non-string or empty `status`. An unknown string `status` is accepted as raw data, and Phase 2 maps it (§9).
  - Classification keys in the input (`category`, `priority`, `is_core`, …) are dropped, and `FileClassification` rejects unknown fields.
  - `state.py` imports cleanly, and `StateGraph` accepts the state with its input and output schemas.
  - These run as short `uv run python -c ...` checks. The test file comes in Phase 2 (PLAN.md §13).
  - Report the results, then stop for review.

**Out of scope for Phase 1:** reading the input file and exit codes (Phase 7), `config.py` (Phase 2), and any LLM code.

---

## Phase 2: Deterministic layer

**Goal:** every deterministic decision from PLAN.md is a pure, tested function: what `parse_pr` computes, the hard routing rules, scoring, and the draft checks. None of them calls an LLM or does I/O, except `load_core_patterns`, which is given the file text.
**Files:** `pyproject.toml` (pytest), `src/agent_workflow/config.py`, `src/agent_workflow/project_context.md` (core paths only), `src/agent_workflow/preprocessing.py`, `src/agent_workflow/router.py` (rules only), `src/agent_workflow/scoring.py`, `tests/test_pipeline.py`.
**Design references:** PLAN.md §3.1–3.3, §6, §8 (`validate`, `finalize`), §9 (input handling, the fallback review, scans).
**Conventions:** functions take explicit arguments (no globals besides `config` constants), return new objects, and never mutate their inputs. Warnings are returned as `list[str]` for `reasoning_trace`, and the nodes in Phase 3 write them.

### Sub-tasks

- [ ] **2.1 Dev dependency**
  - `uv add --group dev pytest`.
  - *Done when:* `uv run pytest --version` works, and `uv sync --no-dev` doesn't install pytest.

- [ ] **2.2 `config.py`: the deterministic constants only**
  - Noise filter: lockfile, generated and vendored globs (§3.1), and the binary markers.
  - Categories: test, docs and config patterns (§3.1), and the known-code extensions used for `is_unknown_type` (`.py .pyi .js .jsx .ts .tsx .go .rs .java .kt .rb .php .c .h .cpp .hpp .cs .swift .scala .sh .sql .html .css .scss`).
  - Sensitive substrings (§3.2), the priority weights (§3.2), `MUST_REVIEW_TOP_N = 5`, and `ARCH_DIFF_BUDGET_TOKENS = 40_000`.
  - The tool-budget formula constants (base 8, 2 per code file, clamp 8–30).
  - Routing thresholds: `MAX_CODE_FILES = 3`, `MAX_CODE_LINES = 50`.
  - Complexity weights (§8), the confidence penalties and caps, and the clamp `[0.1, 0.95]`. The fallback confidence is `0.2`.
  - `CHARS_PER_TOKEN = 4` and `PROJECT_CONTEXT_PATH`.
  - *Not here:* models, effort levels, the Jev threshold and the environment overrides. Those are added in Phases 4 and 6, when something uses them.
  - *Done when:* it imports, and every number above appears in exactly one place.

- [ ] **2.3 `project_context.md`: core paths only**
  - A `## Core paths` section with one backticked glob per bullet: `src/flask/app.py`, `src/flask/sansio/*.py`, `src/flask/ctx.py`, `src/flask/globals.py`, `src/flask/wrappers.py`, `src/flask/sessions.py`, `src/flask/helpers.py`, `src/flask/config.py`.
  - The project conventions are added in Phase 5.
  - `preprocessing.load_core_patterns(markdown: str) -> list[str]` reads the bullets of that section only, and returns `[]` if the section is missing.
  - *Done when:* the loader returns the 8 globs from the file, and `[]` for text without the section.

- [ ] **2.4 `preprocessing.py`: per-file functions**
  - `map_status(raw: str) -> tuple[FileStatus, bool]`: case-insensitive and stripped. `removed` → `deleted`, anything unknown → `("modified", True)`.
  - `count_diff_lines(diff: str) -> tuple[int, int] | None`: counts `+` and `-` lines **inside hunks only**, after the first `@@`. It returns `None` when there's no hunk.
  - `filter_reason(path: str, status: FileStatus, diff: str) -> str | None`: the §3.1 rows in order, returning one of `"lockfile/generated/vendored"`, `"pure rename"`, `"binary or empty"`, `"formatting only"` or `"import only"`.
    - **Formatting only:** in each hunk, the added and removed lines are equal **in order** after normalizing each line to `leading_indent + " ".join(line.split())`. There must be at least one changed line. A dedent or a reordering is never formatting.
    - **Import only:** `.py`/`.pyi` only. Every non-blank changed line fully matches `^\s*(import\s+[\w.]+(\s+as\s+\w+)?(\s*,\s*[\w.]+(\s+as\s+\w+)?)*|from\s+[.\w]+\s+import\s+[\w\s,.*()]+?)\s*(#.*)?$`, with at least one such line, so `import os; os.system(cmd)` is not import-only.
  - `categorize(path: str, filter_reason: str | None) -> FileCategory`, with first match winning (§3.1).
  - `classify_file(file: ChangedFile, core_patterns: Sequence[str]) -> tuple[FileClassification, list[str]]`. It sets every `FileClassification` field per §3.1–3.2 and §9:
    - a malformed diff is a non-empty diff with no hunk and no filter reason, and keeps the reported counts,
    - it warns on an unknown status, on reported counts that don't match the diff, and on a malformed diff.
  - *Done when:* each case in 2.8's per-file table gives the expected fields.

- [ ] **2.5 `preprocessing.py`: PR-level functions**
  - `merge_duplicate_paths(pr: PRInput) -> tuple[PRInput, list[str]]`: for each repeated path, it keeps the first entry's position and status, joins the diffs with `"\n"`, sums the reported counts, and warns once per path.
  - `scan_pr(pr: PRInput) -> list[ScanHit]`:
    - **Secrets:** added lines of every file, mechanical ones included. The patterns are an AWS key `AKIA[0-9A-Z]{16}`, a private key header `-----BEGIN [A-Z ]*PRIVATE KEY-----`, and a quoted assignment `(api[_-]?key|secret|token|passw(?:or)?d)[\w-]*["']?\s*\]?\s*[:=]\s*["'][^"'\s]{8,}["']` (case-insensitive).
    - The assignment pattern matches `SECRET_KEY = "…"`, `app.config["SECRET_KEY"] = "…"`, `{"password": "…"}`, `AWS_SECRET_ACCESS_KEY="…"` and `api_key = "…"`. It does not match `os.environ[...]` lookups, empty values or short values.
    - **Injection:** the title, the description and added lines. The phrases are "ignore/disregard (all/any) previous/prior/above instructions", "you are now", "system prompt", "skip (the) review", "do not review", "approve this (PR/pull request)", and "mark this/it as safe/low risk".
    - `ScanHit.pattern` holds the rule name, never the matched text.
  - `compute_stats(pr, classifications, scan_hits) -> PRStats`:
    - code files are non-filtered source, config and test files,
    - `code_lines_changed` sums their recomputed counts,
    - `estimated_tokens = ceil((len(title) + len(description) + diffs of non-filtered files) / CHARS_PER_TOKEN)`.
  - `build_review_plan(classifications, stats, scan_hits) -> ReviewPlan`:
    - `ranked_files`: non-filtered files, sorted by priority (descending), then by path,
    - `must_review`: core, sensitive and new-module files, scan-hit files (filtered ones included), and the top 5 ranked files, deduplicated, with ranked order first,
    - the budgets come from §3.2.
  - `preprocess(pr: PRInput, core_patterns: Sequence[str]) -> Preprocessed`. `Preprocessed` is a frozen dataclass with `pr`, `classifications`, `stats`, `plan` and `notes`. This is the single entry point `parse_pr` calls in Phase 3.
    - `notes` collects every warning, plus "`pr_description` missing" or "`author` missing", detected through `model_fields_set`.
    - It also warns when the top-level totals (`files_changed`, `total_additions`, `total_deletions`) don't match the recomputed ones.

- [ ] **2.6 `router.py`: hard rules only**
  - `apply_hard_rules(classifications, stats) -> RouteDecision | None`, with `source="rule"`. Rules are checked in this order:
    1. any scan hit → `complex`,
    2. any core, sensitive or new-module file, or a deleted source file → `complex`,
    3. more than 3 code files, or more than 50 changed code lines → `complex`,
    4. any unknown-type or malformed file → `complex`,
    5. only docs and/or mechanical files → `simple`,
    6. otherwise → `None`, meaning the **uncertain middle**, which goes to Jev in Phase 6.
  - A `complex` reason lists every matching rule with its files, for example "core file `src/flask/app.py`; new module `src/flask/rate_limiter.py`".
  - *Not here:* the Jev client and the "Jev unavailable → complex" fallback. The `classify_pr` node adds the fallback in Phase 3, and the client comes in Phase 6.

- [ ] **2.7 `scoring.py`**
  - `complexity_score(classifications) -> float`, per §8:
    - L = recomputed changed lines of non-filtered source and config files, and n = non-filtered source files,
    - each term is clamped to `[0, max]`, the total is clamped to `[1, 10]`, and the result is rounded half-up to 0.5.
  - `ConfidenceSignals`, a frozen dataclass of the §8 inputs:
    - `must_review_coverage: float | None` (None on the simple path),
    - `truncated: bool`,
    - `disagreement_levels: int | None`,
    - `agent_hit_limit: bool`,
    - `validation_retried: bool` and `validation_still_failing: bool`,
    - `core_files: int` and `unverified_findings: int`,
    - `architecture_degraded: bool` and `risk_degraded: bool`.
  - `calibrate_confidence(base: float, signals: ConfidenceSignals) -> tuple[float, list[str]]`: it subtracts all the penalties, then applies all the caps with `min()`, then clamps to `[0.1, 0.95]`. It returns one trace line per adjustment.
  - `check_draft(draft: ReviewDraft, *, changed_paths: set[str], route: RouteDecision, risk: RiskFindings | None, scan_hits: list[ScanHit]) -> list[str]`, which applies the three §8 checks. An empty list means the draft passes.
  - `fallback_review(classifications, plan, stats) -> ReviewDraft`, per §9:
    - the focus areas are the first 6 `ranked_files`, or the first 6 changed paths if every file is mechanical (`ReviewDraft` allows 1–6),
    - the reasoning text states that the review is a deterministic fallback, and confidence is 0.2.
  - *Not here:* output assembly (`"path: concern"` strings, `Step N:` numbering, the split-PR and "not inspected" focus areas). That's the `finalize` node in Phase 3.

- [ ] **2.8 `tests/test_pipeline.py`: one module, no API key**
  - **Scenarios** (one parametrized test), each built in code from small helper functions:

    | Scenario | Expected |
    |---|---|
    | The sample `pr_input.json` | categories source, source, test; `app.py` core; `rate_limiter.py` new module; priorities ≈ 6.80 / 5.23 / 0.57; route `complex` (rule: core + new module); `must_review` = all 3 files; `max_tool_calls` = 14; complexity 5.5 |
    | Docs only (`README.md`, `docs/x.rst`) | route `simple` (rule); complexity 1.0; `max_tool_calls` = 8 |
    | A one-line comment fix in `src/flask/debughelpers.py` | not core, not sensitive; `apply_hard_rules` returns `None` (uncertain middle); complexity 1.5 |
    | 60 source files, 10 changed lines each, no core files | route `complex` (rule: size); `max_tool_calls` = 30; `must_review` = top 5; complexity 6.0 |
    | Docs only with a sensitive-looking name (`docs/config.rst`) | not sensitive; route `simple` (rule) |
  - **Per-file table** (one parametrized test over `classify_file`): a lockfile, a pure rename with an empty diff (→ `"pure rename"`), an empty diff, a binary marker, trailing-whitespace-only (→ formatting), **a dedent (→ not filtered)**, **two swapped lines (→ not filtered)**, import only, **`import os; os.system(cmd)` (→ not filtered)**, an import change in a `.js` file (→ not filtered), a malformed diff, an unknown status (`copied`), reported counts that don't match the diff, an unknown extension (`.xyz`), and a deleted source file.
  - **Focused checks:**
    - `merge_duplicate_paths` and its warning,
    - secrets: each assignment form listed in 2.5 gives a hit, and each listed non-match gives none. A secret in a docs file → a scan hit and route `complex`, with the secret text absent from every output,
    - an injection phrase in the description → a scan hit,
    - `calibrate_confidence` with no signals, with one penalty, with one cap, and at both clamp bounds,
    - `check_draft`: an unknown focus file, a risk level below a verified finding, a secret hit with a non-high risk level, and a blank field,
    - `fallback_review` validates as a `ReviewDraft`.
  - *Done when:* `uv run pytest -q` passes, and the results are reported.

- [ ] **2.9 Phase check**
  - `uv run pytest -q` passes, `py_compile` is clean, and `git diff` is inspected.
  - Report the four scenario results, then stop for review.

**Out of scope for Phase 2:** graph nodes and wiring (Phase 3), the API-surface extraction (Phase 4, its first consumer), the Jev client (Phase 6), the file reading, size limits and exit codes (Phase 7), and the conventions in `project_context.md` (Phase 5).

## Phase 3: Graph skeleton
**Files:** `graph.py`, `nodes.py` (LLM nodes stubbed).
**Check:** it compiles, prints Mermaid, and both paths run end to end on stubs.
*Sub-tasks: to be written when the phase starts.*

## Phase 4: LLM foundation
**Files:** `llm.py`, `prompts.py`, the API-surface extraction in `preprocessing.py` (§3.4), `architecture_analyst`, `synthesize` (both modes).
**Check:** structured output, effort, fallbacks and caching are verified against the installed `langchain-anthropic`, then the first real run on the sample.
*Sub-tasks: to be written when the phase starts.*

## Phase 5: ReAct risk agent (`create_agent`)
**Files:** `tools.py` (`@tool` functions, `ToolInputError`, the `on_error` handler), `project_context.md`, `risk_agent.py` (`RiskAgentState`, `RiskAgentContext`, the middleware, budgets, wrapper, recovery call, hallucination guards, coverage check).
**Check:**
- the agent prints its own Mermaid,
- the tool calls make sense on the sample,
- thinking blocks survive a multi-turn run with `ProviderStrategy` (§5),
- a tiny tool budget ends with findings, with blocked calls getting the limit message,
- a tiny model-call limit triggers the recovery call,
- a bad path or regex comes back as a recoverable error,
- `files_inspected` is filled.
*Sub-tasks: to be written when the phase starts.*

## Phase 6: Jev router
**Files:** `router.py` (Jev client, threshold, fallbacks).
**Check:** read the official Jev docs first. The uncertain-middle PRs are routed with and without `JEV_API_KEY`, a simulated timeout falls back to `complex`, and the trace shows the decision source.
*Sub-tasks: to be written when the phase starts.*

## Phase 7: Degradation & CLI
**Files:** `analyze_pr.py`, the per-node fallbacks.
**Check:** simulated failures produce degraded output with exit code 3, and invalid input exits with 2.
*Sub-tasks: to be written when the phase starts.*

## Phase 8: Langfuse tracing (dev only)
**Files:** `observability.py`.
**Check:** a nested trace appears in the Langfuse UI, and the app still runs without the keys.
*Sub-tasks: to be written when the phase starts.*

## Phase 9: Deliverables
**Files:** `code_review_output.json`, `requirements.txt`, `README.md`.
**Check:** the README covers the design, trade-offs, the Jev decision and fallback, AI-tool usage, and what was new to you.
*Sub-tasks: to be written when the phase starts.*
