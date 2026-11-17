# Implementation Phases

This file sets the build order, the check for each phase, and progress.
`docs/PLAN.md` is the source of truth for the design. This file adds no scope.

**How we work:** one phase at a time. Each phase ends with something runnable that we review before moving on.
A phase is broken into sub-tasks before we start it, and the sub-tasks are reviewed first.

## Overview

| Phase | Goal | Done when | Status |
|---|---|---|---|
| 0 | Housekeeping | `.env` is gitignored | ✅ Done |
| 1 | Setup & schemas | The sample loads and validates, and bad inputs give clear errors | ✅ Done |
| 2 | Deterministic layer | Preprocessing, routing rules and scoring give correct results on the sample and synthetic PRs | ✅ Done |
| 3 | Graph skeleton | The graph compiles, prints Mermaid, and both paths run on stubs | ✅ Done |
| 4 | LLM foundation | First real run on the sample | ✅ Done |
| 5 | ReAct risk agent (`create_agent`) | The agent loop works, stops on budget, and recovers from tool errors | — |
| 6 | Jev router | The uncertain middle is routed with and without `JEV_API_KEY` | — |
| 7 | Degradation & CLI | Simulated failures produce degraded output with exit code 3 | — |
| 8 | Langfuse tracing (dev only) | A nested trace appears in the Langfuse UI | ✅ Done (early, before Phase 4) |
| 9 | Deliverables | `code_review_output.json`, `requirements.txt` and the README are done | — |

---

## Phase 0: Housekeeping ✅
- [x] Add `.env` to `.gitignore`.

---

## Phase 1: Setup & schemas ✅

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
  - *Changed later (Phase 2 code review):* `ChangedFile.diff` defaults to `""` and accepts `null`; `Finding.verified` defaults to `False`, and any `verified` in the input is dropped, so only the hallucination guards can mark a finding verified (with `model_copy`). The focus-area limit is `config.MAX_FOCUS_AREAS` (6).
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

## Phase 2: Deterministic layer ✅

**Goal:** every deterministic decision from PLAN.md is a pure, tested function: what `parse_pr` computes, the hard routing rules, scoring, and the draft checks. None of them calls an LLM or does I/O, except `load_core_patterns`, which is given the file text.
**Files:** `pyproject.toml` (pytest), `src/agent_workflow/config.py`, `src/agent_workflow/project_context.md`, `src/agent_workflow/preprocessing.py`, `src/agent_workflow/router.py` (rules only), `src/agent_workflow/scoring.py`, `tests/test_pipeline.py`.
**Design references:** PLAN.md §3.1–3.3, §6, §8 (`validate`, `finalize`), §9 (input handling, the fallback review, scans).
**Conventions:** functions take explicit arguments (no globals besides `config` constants), return new objects, and never mutate their inputs. Warnings are returned as `list[str]` for `reasoning_trace`, and the nodes in Phase 3 write them.

### Sub-tasks

- [x] **2.1 Dev dependency**
  - `uv add --group dev pytest`.
  - *Done when:* `uv run pytest --version` works, and `uv sync --no-dev` doesn't install pytest.

- [x] **2.2 `config.py`: the deterministic constants only**
  - Noise filter: lockfile, generated and vendored globs (§3.1), and the binary markers.
  - Categories: test, docs and config patterns (§3.1), and the known-code extensions used for `is_unknown_type` (`.py .pyi .js .jsx .ts .tsx .go .rs .java .kt .rb .php .c .h .cpp .hpp .cs .swift .scala .sh .sql .html .css .scss`).
  - Sensitive substrings (§3.2), the priority weights (§3.2), `MUST_REVIEW_TOP_N = 5`, and `ARCH_DIFF_BUDGET_TOKENS = 40_000`.
  - The tool-budget formula constants (base 8, 2 per code file, clamp 8–30).
  - Routing thresholds: `MAX_CODE_FILES = 3`, `MAX_CODE_LINES = 50`.
  - Complexity weights (§8), the confidence penalties and caps, and the clamp `[0.1, 0.95]`. The fallback confidence is `0.2`.
  - `CHARS_PER_TOKEN = 4` and `PROJECT_CONTEXT_PATH`.
  - *Not here:* models, effort levels, the Jev threshold and the environment overrides. Those are added in Phases 4 and 6, when something uses them.
  - *Done when:* it imports, and every number above appears in exactly one place.

- [x] **2.3 `project_context.md`: core paths and Flask conventions**
  - *Decision changed:* it was first planned as core paths only, with the conventions added in Phase 5. At your request, the file was written after reading the Flask source and docs (`main` at `49943b5`), so it already includes the conventions. Phase 5 only consumes them.
  - A `## Core paths` section with one backticked glob per bullet. It holds the original 8 (`src/flask/app.py`, `src/flask/sansio/*.py`, `src/flask/ctx.py`, `src/flask/globals.py`, `src/flask/wrappers.py`, `src/flask/sessions.py`, `src/flask/helpers.py`, `src/flask/config.py`) plus 4 added after reading the repo: `src/flask/__init__.py` (the public API), `src/flask/blueprints.py`, `src/flask/templating.py` (autoescaping) and `src/flask/json/*.py` (the session serializer). In the globs, `*` stays within one folder.
  - Convention sections, one topic per `##` heading for `read_project_context(topic)`: architecture, setup phase vs request handling, concurrency and state, scope, extensions and configuration, security, public API and compatibility, contribution and testing.
  - `preprocessing.load_core_patterns(markdown: str) -> list[str]` reads only the bullets of that section that consist of a single backticked glob, and returns `[]` if the section is missing.
  - *Done when:* the loader returns the 12 globs from the file, and `[]` for text without the section.

- [x] **2.4 `preprocessing.py`: per-file functions**
  - `map_status(raw: str) -> tuple[FileStatus, bool]`: case-insensitive and stripped. `removed` → `deleted`, anything unknown → `("modified", True)`.
  - `count_diff_lines(diff: str) -> tuple[int, int] | None`: counts `+` and `-` lines **inside hunks only**, after the first `@@`. It returns `None` when there's no hunk.
  - `filter_reason(path: str, status: FileStatus, diff: str) -> str | None`: the §3.1 rows in order, returning one of `"lockfile/generated/vendored"`, `"pure rename"`, `"binary or empty"`, `"formatting only"` or `"import only"`.
    - **Formatting only:** in each change block (a run of `-`/`+` lines between context lines), the removed and added lines are equal **in order** after `rstrip()` only. There must be at least one changed line. A dedent, a reordering, a line moved past a context line, or an inner-whitespace change (e.g. inside a string) is never formatting.
    - *Decision changed:* the plan first collapsed inner runs of whitespace too. That could hide a real change such as `sep = "  "` → `sep = " "`, so only trailing whitespace is ignored now (PLAN.md §3.1 updated).
    - **Import only:** `.py`/`.pyi` only. Every non-blank changed line fully matches `^\s*(import\s+[\w.]+(\s+as\s+\w+)?(\s*,\s*[\w.]+(\s+as\s+\w+)?)*|from\s+[.\w]+\s+import\s+[\w\s,.*()]+?)\s*(#.*)?$`, with at least one such line, so `import os; os.system(cmd)` is not import-only.
  - `categorize(path: str, filter_reason: str | None) -> FileCategory`, with first match winning (§3.1).
    - *Decision changed:* `requirements*.txt` (`CONFIG_BEFORE_DOCS_NAME_GLOBS`) is config, checked before the docs `*.txt` rule. Routing rules are unchanged; whether a dependency-only PR needs its own routing rule is decided separately.
  - `classify_file(file: ChangedFile, core_patterns: Sequence[str]) -> tuple[FileClassification, list[str]]`. It sets every `FileClassification` field per §3.1–3.2 and §9:
    - a malformed diff is a non-empty diff with no hunk and no filter reason, and keeps the reported counts (so does any other diff without a hunk: empty, binary, pure rename),
    - *Decision changed (code review):* an empty diff with reported changes is a **missing patch**: not filtered, flagged `is_malformed`, reported counts kept. A `null` or missing `diff` becomes `""` in `ChangedFile` (PLAN.md §9).
    - *Decision changed (Phase 2 quality review):* a rename with no hunk but reported changes is a missing patch too. An **added or deleted** file is never formatting-, import- or empty-only (only lockfile/generated/vendored or binary), so deleting an import-only module or adding an empty `__init__.py` routes `complex`. `__init__.py`/`__init__.pyi` are never import-only, because their imports are the public API.
    - *Fixed (code review):* diffs are split on `"\n"` only (not `splitlines()`), and a file header inside a diff (`diff --git`, or `--- `/`+++ ` followed by `@@`) ends the current hunk, so joined diffs don't count headers as changes.
    - `is_core` comes from the path alone, also for filtered files; `*` in a core glob doesn't cross `/`, and a leading `./` or `/` is ignored,
    - name and folder patterns for categories are case-insensitive,
    - it warns on an unknown status, on reported counts that don't match the diff, and on a malformed diff.
  - *Done when:* each case in 2.8's per-file table gives the expected fields.

- [x] **2.5 `preprocessing.py`: PR-level functions**
  - `merge_duplicate_paths(pr: PRInput) -> tuple[PRInput, list[str]]`: for each repeated path (compared after normalising, so `./src/a.py` = `src/a.py`), it keeps the first entry's path, position and status, joins the diffs with `"\n"`, sums the reported counts, and warns once per path.
  - `scan_pr(pr: PRInput) -> list[ScanHit]`:
    - **Secrets:** added lines of every file (mechanical ones included), the title and the description. The rules are an AWS key `AKIA[0-9A-Z]{16}`, a private key header `-----BEGIN [A-Z ]*PRIVATE KEY-----`, a quoted assignment, and a hard-coded `os.environ.get(…)`/`os.getenv(…)` default. The exact regexes are in `preprocessing.py` (`_SECRET_RULES`).
    - The assignment rule matches `SECRET_KEY = "…"`, `app.config["SECRET_KEY"] = "…"`, `{"password": "…"}`, `AWS_SECRET_ACCESS_KEY="…"`, `api_key = "…"`, `csrf_token = "…"`, `API_KEY: str = "…"` and camelCase names ending in a keyword (`clientSecret`, `stripeApiKey`, `dbPassword`, `accessToken`; added in the Phase 2 quality review). It does not match `os.environ[...]` lookups, empty values, short values, or non-secret names such as `tokenizer`, `myTokenizer`, `isSecretValid`, `token_url` and `password_reset_template`.
    - *Decision changed (code review):* the keyword may only be followed by a key-like suffix (`_key`, `_access_key`, `_token`, `_secret`), annotated assignments and `environ.get` defaults were added, and the title and description are scanned for secrets too.
    - **Injection:** the title, the description and added lines. The phrases are "ignore/disregard (all/any) previous/prior/above instructions", "you are now", "system prompt", "skip (the) review", "do not review", "approve this (PR/pull request)", and "mark this/it as safe/low risk".
    - `ScanHit.pattern` holds the rule name, never the matched text.
    - One hit per (kind, file, rule), not one per matching line. Words in parentheses in the phrase list are optional. Two small widenings: an optional "the" in "ignore the previous instructions", and "low-risk" with a hyphen. A diff without hunks (malformed) is scanned as raw text. Removed lines are never scanned.
  - `compute_stats(pr, classifications, scan_hits) -> PRStats`:
    - code files are non-filtered source, config and test files,
    - `code_lines_changed` sums their recomputed counts,
    - `estimated_tokens = ceil((len(title) + len(description) + diffs of non-filtered files) / CHARS_PER_TOKEN)`.
  - `build_review_plan(classifications, stats) -> ReviewPlan` (scan hits come from `stats.scan_hits`):
    - `ranked_files`: non-filtered files, sorted by priority (descending), then by path,
    - `must_review`: core, sensitive and new-module files, scan-hit files (filtered ones included), and the top 5 ranked files, deduplicated, with ranked order first,
    - a filtered file reaches `must_review` only through a scan hit (it has nothing else to review), and such files come after the ranked ones,
    - the budgets come from §3.2.
  - `preprocess(pr: PRInput, core_patterns: Sequence[str]) -> Preprocessed`. `Preprocessed` is a frozen dataclass with `pr`, `classifications`, `stats`, `plan` and `notes`. This is the single entry point `parse_pr` calls in Phase 3.
    - `notes` collects every warning, plus "`pr_description` is missing or empty" and the same for `author`. Missing, `null` and empty values get the same note.
    - It also warns when the top-level totals (`files_changed`, `total_additions`, `total_deletions`) don't match the recomputed ones. `files_changed` may count either the input entries or the merged files.

- [x] **2.6 `router.py`: hard rules only**
  - `apply_hard_rules(classifications, stats) -> RouteDecision | None`, with `source="rule"`. Rules are checked in this order:
    1. any scan hit → `complex`,
    2. any core, sensitive or new-module file, or a deleted source file → `complex`,
    3. more than 3 code files, or more than 50 changed code lines → `complex`,
    4. any unknown-type or malformed file → `complex`,
    5. only docs and/or mechanical files → `simple`,
    6. otherwise → `None`, meaning the **uncertain middle**, which goes to Jev in Phase 6.
  - A `complex` reason lists every matching rule with its files, for example "core file `src/flask/app.py`; new module `src/flask/rate_limiter.py`; size: 151 changed code lines > 50". Each rule names at most `ROUTE_REASON_MAX_PATHS = 5` files (then "+N more"), and scan hits show the rule name and location only.
  - *Decision:* rules 2 and 4 count **non-filtered files only**, consistent with `must_review` in 2.5. A formatting- or import-only change to a core file routes `simple` (rule 5). A scan hit counts in any file, filtered ones included.
  - *Decision:* no special rule for dependency-only PRs. A `requirements.txt`-only PR lands in the uncertain middle (`complex` without Jev); a lockfile-only PR stays `simple`.
  - *Not here:* the Jev client and the "Jev unavailable → complex" fallback. The `classify_pr` node adds the fallback in Phase 3, and the client comes in Phase 6.

- [x] **2.7 `scoring.py`**
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
  - `check_draft(draft: ReviewDraft, *, classifications: Mapping[str, FileClassification], route: RouteDecision, risk: RiskFindings | None, scan_hits: list[ScanHit]) -> list[str]`, which applies the three §8 checks. An empty list means the draft passes.
    - *Decision:* a secret hit in a **test file** doesn't force `high` (fixtures use fake keys); it still routes `complex`, is in `must_review` and is shown to the model. Hits anywhere else, including the title, description and docs, still force `high`. `classifications` replaces `changed_paths` so the check can tell test files apart.
    - *Decision:* focus-area paths are compared after normalising (`./src/a.py` = `src/a.py`).
  - `fallback_review(classifications, plan, stats) -> ReviewDraft`, per §9:
    - risk level per PLAN.md §9 (`high` on a secret hit, `medium` on non-filtered core/sensitive/new-module/deleted source files or an injection hit, otherwise `low`); a secret hit in a test file counts as `medium`,
    - the focus areas are `must_review` first, then the rest of `ranked_files`, up to 6, or the first 6 changed paths if every file is mechanical (`ReviewDraft` allows 1–6). *Changed:* `must_review` first, so a filtered file with a scan hit (e.g. a secret in a lockfile) is never left out,
    - the reasoning text states that the review is a deterministic fallback, and confidence is 0.2.
  - *Not here:* output assembly (`"path: concern"` strings, `Step N:` numbering, the split-PR and "not inspected" focus areas). That's the `finalize` node in Phase 3.

- [x] **2.8 `tests/test_pipeline.py`: one simple test, no API key**
  - *Decision changed:* the assignment says "No extensive test suite (one simple test is fine)", so 2.8 is **one parametrized test** of the deterministic layer (`preprocess` → `apply_hard_rules` → `complexity_score`), not the earlier per-file table and focused checks. The detailed checks run as an ad-hoc development script outside the repo and aren't submitted.

    | Scenario | Expected |
    |---|---|
    | The sample `pr_input.json` | route `complex` (core + new module); complexity 5.5; `max_tool_calls` = 14; `must_review` = all 3 files |
    | Docs only (`README.md`, `docs/config.rst`) | route `simple`; complexity 1.0; `max_tool_calls` = 8 |
    | A one-line comment fix in `src/flask/debughelpers.py` | `None` (uncertain middle); complexity 1.5; `max_tool_calls` = 10 |
    | 60 source files, 10 changed lines each | route `complex` (size); complexity 6.0; `max_tool_calls` = 30; `must_review` = top 5 |
    | A secret in a docs file | route `complex`; the secret text is absent from every output |
  - Every scenario also asserts that the decision source is `"rule"` and that no secret text reaches the classifications, stats, plan, notes or route reason.
  - *Done when:* `uv run pytest -q` passes, and the results are reported.

- [x] **2.9 Phase check**
  - `uv run pytest -q` passes, `py_compile` is clean, and `git diff` is inspected.
  - Report the scenario results, then stop for review.

**Out of scope for Phase 2:** graph nodes and wiring (Phase 3), the API-surface extraction (Phase 4, its first consumer), the Jev client (Phase 6), the file reading, size limits and exit codes (Phase 7).

## Phase 3: Graph skeleton ✅

**Goal:** the full 7-node graph from PLAN.md §2 is wired and runs end to end, with no API key. The deterministic nodes (`parse_pr`, `classify_pr`, `validate`, `finalize`) are real. The three LLM nodes (`architecture_analyst`, `risk_agent`, `synthesize`) are **stubs** that return fixed, schema-valid results. Phases 4–5 replace only the stub bodies; the wiring, the state and the deterministic nodes stay as they are.
**Files:** `src/agent_workflow/nodes.py` (new), `src/agent_workflow/graph.py` (new), `config.py`, `router.py`, `scoring.py`, `state.py` (3.2), `tests/test_pipeline.py` (3.9), `docs/PLAN.md` (3.10).
**Design references:** PLAN.md §2 (graph, LangGraph features), §3.3 (route trace lines), §3.4 (coverage check, split-PR focus area), §6 (state), §8 (`validate`, `finalize`), §13 (file layout).
**Conventions:**
- A node takes `state: ReviewState` and returns a partial-update `dict`. Nodes only call the Phase 2 functions and the helpers below, so every decision stays a pure, testable function outside the graph.
- Trace lines are plain sentences with no numbering; `finalize` numbers them. No trace line holds matched secret text (Phase 2 already keeps it out of every derived object).
- `degraded` entries are node names (`"architecture_analyst"`, `"risk_agent"`, `"synthesize"`). Phase 7 writes them; `finalize` already reads them.

**Checked before writing these sub-tasks** (langgraph 1.2.19, a scratch probe outside the repo):
- a conditional edge that returns a list (`["architecture_analyst", "risk_agent"]`) fans out to both analysts,
- `add_edge(["architecture_analyst", "risk_agent"], "synthesize")` runs `synthesize` once, after both,
- the `validate → synthesize` retry edge doesn't re-trigger that join,
- `get_graph().draw_mermaid()` draws every edge, with the conditional ones dashed.

### Sub-tasks

- [x] **3.0 Pre-phase review (your workflow)**: *skipped (decision)*
  - Run the code-review and code-simplifier plugins on the current code (Phase 2, `7215260`), apply the validated findings, and confirm `uv run pytest -q` still passes.
  - *Decision:* skipped. The Phase 2 code already went through a code review and a quality review before it was merged (see "Decision changed (code review)" and "(Phase 2 quality review)" in Phase 2), and nothing has changed since. The review runs again before Phase 4, on the Phase 3 code.

- [x] **3.1 `config.py`: the constants this phase uses**
  - `MAX_VALIDATION_RETRIES = 1` (§8: "at most 1 retry").
  - `SPLIT_PR_MAX_SOURCE_FILES = 40` and `SPLIT_PR_MAX_MODULES = 6`. Both are strict `>`, and **both** must be exceeded.
    - *Changed (pre-3.5 code review):* first named `SPLIT_PR_MIN_*`, which reads as "at least". Renamed to `MAX`, matching `MAX_CODE_FILES = 3` ("more than 3 → complex").
    - *Decision changed:* PLAN.md §3.4 said "above 40 source files **or** 6 modules". With "or", a small PR spread over 7 folders (e.g. a rename across packages) would get "consider splitting", so it's now **"and"**: only a PR that is both large and spread out gets the advice. PLAN.md §3.4 is updated.
  - *Done when:* each number appears exactly once and is imported where it's used.

- [x] **3.2 `state.py`: `risk_budget_exhausted` (a plan discrepancy, approved)**
  - **The issue:** PLAN.md §6 says the risk wrapper maps `budget_exhausted` back to the main state, and §8 caps confidence on it ("Agent hit its limit"), but `ReviewState` has no field to hold it.
  - **Proposed:** add `risk_budget_exhausted: bool` to `ReviewState`. `parse_pr` sets it to `False`, the risk stub returns `False`, and Phase 5 sets the real value. PLAN.md §6's state block is updated to match (3.10), which fixes an internal inconsistency rather than changing the design.
  - *Rejected alternative:* a hidden system field on `RiskFindings` (like `verified`). It mixes run metadata into the findings model the LLM fills.

- [x] **3.3 `router.py`: the full routing decision and its trace line**
  - `decide_route(classifications: Mapping[str, FileClassification], stats: PRStats) -> RouteDecision`:
    - returns `apply_hard_rules(...)` when the rules decide,
    - otherwise (the uncertain middle) `RouteDecision(route="complex", source="fallback", reason="uncertain middle; Jev unavailable")`.
    - Phase 6 inserts the Jev call between these two steps, so the `classify_pr` node never changes.
  - `describe_route(decision: RouteDecision) -> str`, in the §3.3 format:
    - `"Route: complex (rule: core file `src/flask/app.py`; new module `src/flask/rate_limiter.py`; size: 151 changed code lines > 50)."`
    - `"Route: simple (rule: only docs and/or mechanical files (2 docs, 0 mechanical))."`
    - `"Route: complex (fallback: uncertain middle; Jev unavailable)."`
    - The Jev form, `"Route: simple (Jev, p=0.93): …"`, is added in Phase 6.

- [x] **3.4 `scoring.py`: the pure helpers `finalize` needs**
  - `_unread_must_review(plan, files_inspected) -> dict[str, str]` (normalised path → path as written, in `must_review` order): the single definition of "read", shared by the two functions below so the coverage cap and the focus areas can't disagree. *Added in the pre-3.5 code review.*
  - `must_review_coverage(plan: ReviewPlan, files_inspected: Sequence[str]) -> float | None`: the share of `must_review` files (normalised paths) found in `files_inspected`. `None` when `must_review` is empty (e.g. a complex route caused only by a title/description scan hit), so there is no coverage cap.
  - `confidence_signals(*, route, plan, classifications, architecture, risk, files_inspected, risk_budget_exhausted, revision_count, validation_errors, degraded) -> ConfidenceSignals`, mapping the state to the §8 signals:

    | Signal | Value |
    |---|---|
    | `must_review_coverage` | `must_review_coverage(...)` on the complex path, `None` on the simple path |
    | `truncated` | any non-filtered file `is_malformed`. Phase 4 adds "the architecture diff budget cut files". |
    | `disagreement_levels` | `abs(LEVEL_ORDER[impact_level] − LEVEL_ORDER[max_severity])` when both analysts returned results and `risk.max_severity` isn't `None`; otherwise `None` |
    | `agent_hit_limit` | `risk_budget_exhausted` |
    | `validation_retried` | `revision_count > 0` |
    | `validation_still_failing` | `bool(validation_errors)` (the last `validate` result) |
    | `core_files` | non-filtered `is_core` files |
    | `unverified_findings` | risk findings with `verified == False` (0 when `risk` is `None`). Until Phase 5 adds the guards, every finding counts as unverified, which is the conservative default. |
    | `architecture_degraded` / `risk_degraded` | `"architecture_analyst"` / `"risk_agent"` in `degraded` |

  - `split_pr_recommendation(classifications) -> str | None`: counts the non-filtered source files and their modules, where a module is a distinct parent directory (`posixpath.dirname` of the normalised path). If **both** counts are over their limits (3.1), it returns `"PR-wide: {n} source files across {m} modules; consider splitting it into smaller PRs"`.
  - `final_focus_areas(draft: ReviewDraft, *, route: RouteDecision, plan: ReviewPlan, classifications, files_inspected) -> tuple[list[str], list[str]]` returns (focus strings, trace lines):
    1. the split-PR recommendation first, if there is one (PR-level and the most actionable item for a huge PR),
    2. then the draft's focus areas, in order, as `"{file}: {concern}"`,
    3. **coverage check (complex path only):** for the `must_review` files not in `files_inspected`:
       - an unread file the draft already names gets the suffix `" (verify: not inspected by the risk agent)"` on that item,
       - the unread files the draft doesn't name are listed together in **one** item at the end: `"PR-wide: verify: must-review files `a`, `b` +N more not inspected by the risk agent"`,
    4. the draft's exact duplicates are removed. The PR-wide items (split advice, combined "not inspected") have reserved slots, so when the total is over `MAX_FOCUS_AREAS` (6), the **last draft items** are cut.
    - Trace lines: one listing every unread `must_review` file (via `describe_paths`), and one with the number of draft items cut by the limit, so nothing is dropped silently.
    - *Decision changed (pre-3.5 code review):* the first version added one "not inspected" item per file *after* the draft's items and then cut to 6, so unread files could disappear from the focus areas (PLAN.md §3.4 says each one is flagged). One combined item with a reserved slot always fits. The slot is reserved before deciding which unread files the draft "names", so a draft area cut to make room can't take the only mention of an unread file with it (checked with a regression case).
  - `number_trace(lines: Sequence[str]) -> list[str]`: `["Step 1: …", "Step 2: …", …]`.

- [x] **3.5 `nodes.py`: the four deterministic nodes**
  - `parse_pr(state) -> dict`:
    - `PRInput.model_validate(state["raw_pr"])`. A `ValidationError` propagates; the CLI validates first and exits 2 (Phase 7).
    - It reads `config.PROJECT_CONTEXT_PATH` and calls `load_core_patterns`, then `preprocess`.
    - *Decision:* a missing `project_context.md` raises, with no silent `[]`. Losing the core patterns would silently weaken routing (fewer `complex` routes), and the file ships with the package.
    - Returns `pr`, `classifications`, `stats` and `plan`, and initialises `architecture=None`, `risk=None`, `files_inspected=[]`, `draft=None`, `validation_errors=[]`, `revision_count=0` and `risk_budget_exhausted=False`, so later nodes never hit a missing key.
    - Trace: every `notes` entry, then
      - `"Parsed {files_total} file(s): {code_files} code ({source_files} source), {filtered_files} mechanical skipped; {code_lines_changed} changed code lines; ~{estimated_tokens} tokens."`
      - `"Review plan: must-review {describe_paths('file', must_review)}; tool budget {max_tool_calls} calls; architecture diff budget {architecture_diff_budget_tokens} tokens."` (or `"must-review: none"`).
  - `classify_pr(state) -> dict`: `{"route": decide_route(...), "reasoning_trace": [describe_route(route)]}`.
  - `validate(state) -> dict`:
    - `problems = check_draft(state["draft"], classifications=…, route=…, risk=…, scan_hits=state["stats"].scan_hits)`,
    - returns `validation_errors=problems` and a trace line, `"Validation passed."` or `"Validation failed: {p1}; {p2}."`.
  - `finalize(state) -> dict`:
    1. if `draft` is `None`, it raises `RuntimeError` (Phase 7 replaces this with the fallback review),
    2. `complexity_score(classifications)`,
    3. `confidence_signals(...)`, then `calibrate_confidence(draft.confidence, signals)`,
    4. `final_focus_areas(...)`,
    5. it appends its own trace lines: `"Model confidence {x:.2f}: {confidence_reasoning}"`, the calibration lines, `"Confidence: {final:.2f}."`, `"Complexity score: {score}."`, the focus-area lines, and, if validation is still failing, `"Validation still failing after {revision_count} retry: …"`,
    6. it returns `{"output": ReviewOutput(...)}`, with `reasoning_trace=number_trace(state["reasoning_trace"] + its own lines)`.

- [x] **3.6 `nodes.py`: the three LLM stubs**
  - Each body is marked `# STUB: replaced in Phase 4` (or 5) and makes no network call.
  - `architecture_analyst`: `ArchitectureFindings(impact_level="low", summary="Stub: not analysed (no LLM until Phase 4).", findings=[])`, with the trace line `"Architecture analyst: stub, no LLM call."`.
  - `risk_agent`: `risk=RiskFindings(findings=[])`, `files_inspected=[]` (honest: nothing was read, so the coverage cap applies), `risk_budget_exhausted=False`, with the trace line `"Risk agent: stub, no tools run."`.
  - `synthesize`, the same stub on both paths:
    - the draft is `fallback_review(classifications, plan, stats)` with `confidence=0.8` and `confidence_reasoning="Stub draft (no LLM until Phase 4)."`. A stub-only constant in `nodes.py`, not `config.py`.
    - 0.8 rather than the fallback's 0.2, so the calibration's penalties and caps show up visibly on the stub runs.
    - **Not a stub (kept in Phase 4):** if `validation_errors` is non-empty, this run is a revision: `revision_count += 1`, and the trace says `"Synthesize: revision {n} with the validator's feedback."`.
  - The `validate` → retry edge (3.7) is `"synthesize" if validation_errors and revision_count < MAX_VALIDATION_RETRIES else "finalize"`. With the limit at 1, synthesize runs at most twice.

- [x] **3.7 `graph.py`: `build_graph()`**
  - `build_graph() -> CompiledStateGraph`: `StateGraph(ReviewState, input_schema=GraphInput, output_schema=GraphOutput)`.
  - It looks up each node as `nodes.<name>` when it builds the graph, so a check or test can swap an LLM node by patching the `nodes` module.
  - Edges:
    - `START → parse_pr → classify_pr`,
    - `classify_pr` → `after_classify(state)`, which returns `"synthesize"` or `["architecture_analyst", "risk_agent"]`; path map `["synthesize", "architecture_analyst", "risk_agent"]`,
    - `add_edge(["architecture_analyst", "risk_agent"], "synthesize")`, the join,
    - `synthesize → validate`,
    - `validate` → `after_validate(state)` (3.6); path map `["synthesize", "finalize"]`,
    - `finalize → END`.
  - It compiles with **no checkpointer** (§11).
  - `python -m agent_workflow.graph` prints the Mermaid diagram.
  - *Deferred to Phase 4:* `RetryPolicy` on the LLM nodes. Which exception structured output raises on a schema mismatch must be checked first; retrying on a guessed type would retry nothing, or retry 400s.

- [x] **3.8 Phase check (ad hoc, no API key)**
  - *Result:* every row below passed on the real graph (`build_graph().invoke`), with network connections blocked and each node call counted. LangGraph's debug stream shows both analysts in step 3 and `synthesize` alone in step 4.
  - `uv run python -m agent_workflow.graph` prints Mermaid with all 7 nodes, the dashed fan-out and the dashed retry edge.
  - `build_graph().invoke({"raw_pr": …})` returns only `{"output": ReviewOutput}`:

    | PR | Expected |
    |---|---|
    | The sample `pr_input.json` | `Route: complex (rule: core file …; new module …; size: 151 changed code lines > 50)`; both stubs run once and `synthesize` runs once; `risk_level` `medium`; 3 focus areas, each with the "not inspected" suffix; complexity 5.5; confidence 0.8 − 0.05 (core) = 0.75 → capped at 0.50 (coverage 0%) |
    | Docs only (`README.md`, `docs/config.rst`) | `Route: simple (rule: …)`; neither analyst runs; `risk_level` `low`; 2 focus areas, no suffix; complexity 1.0; confidence 0.80 |
    | One-line comment fix in `src/flask/debughelpers.py` | `Route: complex (fallback: uncertain middle; Jev unavailable)`; source `fallback` |
    | A secret in `README.md` | complex (rule); `risk_level` `high`; validation passes; the secret text is absent from the whole output |
    | Sample, with `nodes.synthesize` patched to name a file not in the PR | `synthesize` runs twice, `"Validation failed"` appears twice, `revision_count` 1, confidence ≤ 0.50 ("still fails validation") |

  - Every trace line starts with `Step N:`, numbered from 1 with no gaps.
  - The relative order of the two analysts' trace lines isn't asserted (they run in the same step).

- [x] **3.9 Test (approved: the graph test)**
  - Add one small function, `test_graph_paths`, to the existing `tests/test_pipeline.py`. It's parametrized over the docs-only PR (simple) and the sample (complex), and asserts the route line, that the output validates and the `Step N:` numbering. It needs no API key. From Phase 4 on, it patches the three LLM nodes with stubs (possible because of 3.7's lookup).
  - *Rejected alternative:* keep Phase 2's "one test" scope, and run the graph checks ad hoc only (3.8).
  - *Why:* AGENTS.md puts graph routing first among what to test, and it's about 20 lines in the same single module, so it stays within "one simple test is fine".

- [x] **3.10 Docs and wrap-up**
  - *Done early (pre-3.5 code review), so the plan matches the code now:* PLAN.md §6 lists `risk_budget_exhausted` (3.2); §3.4 says "and" (3.1) and describes the combined "not inspected" item (3.4); §7, "Coverage check": state that the wrapper only records `files_inspected`, and that `finalize` turns unread `must_review` files into focus areas (3.4). The wrapper runs before the draft exists, so it can't add focus areas itself. *Approved.*
  - PLAN.md §3.4, "Very large PRs": "above 40 source files **and** 6 modules" (3.1).
  - PLAN.md §13: the test module now covers the graph's two paths too (3.9).
  - Tick the sub-tasks here, and set the Phase 3 status in the overview.
  - `uv run pytest -q` passes, `py_compile` is clean, and `git diff` is inspected. Report the 3.8 results, then stop for review.

### Review pass after Phase 3 (code-review + code-simplifier) ✅
Both ran on the whole Phase 3 diff. Validated findings applied; the rest were left unchanged with a reason.
- **Applied, code review:**
  - **#1:** the "Not inspected" trace line now names **every** unread `must_review` file. The combined focus item keeps the 5-name cap ("+N more") to stay readable.
  - **#3:** focus-area duplicates are found by (normalised file, concern), so `./src/a.py: X` and `src/a.py: X` are one item (the first spelling is kept).
  - **#2, option A:** a draft still failing after its retry is **repaired** in `finalize` by `scoring.repair_draft`:
    - the risk level is raised to the minimum the checks require (the highest verified finding, or `high` for a secret hit outside test files; the 2.7 test-file exception holds),
    - focus areas on files not in the PR are dropped,
    - each repair goes to the trace, and the confidence cap of 0.5 applies.
    - `check_draft` and `repair_draft` share the rule helpers (`_changed_paths`, `_highest_verified`, `_forcing_secret_hits`), so they can't drift. `final_focus_areas` now takes the (repaired) focus areas instead of the draft. PLAN.md §8 is updated.
- **Applied, code-simplifier (all 9):** clearer names and lines in `final_focus_areas` and `split_pr_recommendation`; `# STUB:` comments on all three stub bodies; an accurate state-initialisation comment; `files_inspected` read once in `finalize`; stricter, split assertions in `test_graph_paths`.
- **Test:** one compact regression test, `test_repair_failed_draft` (3 cases: secret outside tests → `high`; secret only in a test file → not forced; a focus area on a file not in the PR → dropped).
- **Not changed, with reasons:**
  - **#4:** a scan-hit file that is filtered still counts for coverage, because the agent should read it. *Phase 5 note:* `read_file_diff` must allow filtered files that are in `must_review`.
  - **#5:** `split_pr_recommendation` needs the paths anyway; a root-level file is a real location.
  - **#6:** the graph test's two runs are needed, since `values` mode returns the full internal state, not the `{output}` schema.
  - **#7:** both coverage computations use the same `normalize_path`.
  - **#8:** reading `project_context.md` once per CLI run is intended.

### Final review pass before commit (code-review + code-simplifier) ✅
- **Applied, code review:**
  - **#1:** if the repair drops every focus area, the rule-based focus areas take their place (with a `Repair:` trace line), so the output is never empty.
  - **#2:** `test_graph_paths` has a third case: `synthesize` is patched to name a file not in the PR, so the draft fails twice. It asserts 2 synthesize/validate runs, a `Repair:` line, confidence ≤ 0.5, and that the bad file is gone. Fault injection: `<=` in `after_validate`, a skipped retry, and a missing `revision_count` increment are all caught.
  - **#4:** a raised risk level prefixes the reasoning with `[Repaired: risk level raised from 'x' to 'y': …]`.
  - **#5:** focus areas are written with the PR's own path, not the model's spelling (`./src/a.py`).
  - **#6:** the model's confidence reasoning is collapsed to one line in the trace, so it can't break the `Step N:` format.
  - **#7:** one constant, `NOT_INSPECTED`, for the per-file suffix and the combined item (the output strings are unchanged).
  - **#8:** `repair_draft` splits kept and dropped focus areas in one loop. It now returns the repaired `ReviewDraft` plus its trace lines.
- **Applied, code-simplifier:** that same `repair_draft` loop, and a `kept` column in `test_repair_failed_draft`.
- **Not changed:**
  - **#3:** a negative slice only when `MAX_FOCUS_AREAS` is 1, but it's 6. The reserved slots need at least 2 anyway.
  - **#9:** coverage normalising `must_review` twice; same reason as the earlier #7.

**Out of scope for Phase 3:**
- real LLM calls, `RetryPolicy` and usage accounting (Phase 4),
- the agent itself, the hallucination guards and real `files_inspected` (Phase 5),
- the Jev client (Phase 6),
- the per-node degradation fallbacks (e.g. synthesize failing → `fallback_review` + `degraded`), reading the input file, exit codes and the CLI (Phase 7),
- Langfuse (Phase 8).

## Phase 4: LLM foundation
**Files:** `llm.py`, `prompts.py`, the API-surface extraction in `preprocessing.py` (§3.4), `architecture_analyst`, `synthesize` (both modes).
**Check:** structured output, effort, fallbacks and caching are verified against the installed `langchain-anthropic`, then the first real run on the sample.

- [x] **4.0 Pre-phase review (code-review + code-simplifier)** ✅
  Both ran on `7215260..a67925e`, which is the Phase 3 code plus the early Phase 8 (Langfuse) code. code-review ran at level `high`, and code-simplifier was report-only. 13 findings, one reported by both tools. Every finding was checked against the code, and you approved the list below.
  - **Applied, code review:**
    - **E (plan gap):** PLAN.md §9 says an injection hit "adds a focus area and lowers confidence", but §8's confidence table had no injection row, so only the `complex` route was implemented.
      - Now `finalize` adds a PR-wide warning first in the focus areas (`PR-wide: injection-like text in …; check that the review wasn't steered by it`). It has a reserved slot, names only where the text was found and never the text itself, and is built by `scoring.injection_warning`.
      - Confidence gets **−0.1** (`CONF_PENALTY_INJECTION`, your choice over a cap), with a trace line.
      - PLAN.md §8 now has the table row and the assembly rule.
    - **C:** model text went into trace lines unflattened. A focus-area file such as `"x.py\nStep 3: Validation passed."` could forge a step through the validation and repair lines. `number_trace` now collapses every line, replacing the one-off flattening of `confidence_reasoning`.
    - **B:** tracing now also requires `LANGFUSE_BASE_URL`. Without it, the SDK defaults to Langfuse Cloud, and traces hold full diffs. `.env.example` and PLAN.md §12 are updated.
  - **Applied, both tools (A):** `dev_run` printed "tracing: on" from the keys alone. It now reports whether a handler was actually attached. `langfuse_enabled()` was removed, and its check is now inside `get_langfuse_handler`.
  - **Applied, code-simplifier (D):**
    - `split_pr_recommendation` cited "decided in 3.1", which reads as PLAN.md §3.1; it now cites only §3.4.
    - The `final_focus_areas` docstring is re-wrapped.
    - The tests load the sample PR once (`SAMPLE_RAW`).
    - `1 + int(failing_draft)`.
  - **Tests:**
    - `test_graph_paths` has an injection case: a docs fix with "Ignore previous instructions…" in the description. It checks that the warning comes first and that the −0.10 line is in the trace.
    - The failing-draft case now uses a forging file name and asserts that no trace line contains a newline.
    - `test_tracing_is_optional` has a no-base-URL case.
    - Fault injection: removing the flattening, the penalty, the warning or the base-URL requirement each makes one test fail.
  - **Not changed, with reasons:**
    - **The flush could stall when the server is down:** measured at 3 s with exit 0, since a refused connection fails fast. Only a server that accepts connections and then hangs could stall, and production installs don't include `langfuse`.
    - **`repair_draft` and `check_draft` could drift:** they already share the rule helpers (Phase 3 review), and E adds no risk-level rule.
    - **The graph test runs the graph twice:** decided in the Phase 3 review (#6). Revisit in Phase 4 if the fake LLM nodes are stateful.
    - **The unread must-review set is computed twice:** the same helper is used, as in the Phase 3 review (#7).

**Goal:** `architecture_analyst` and `synthesize` make real Claude calls, and the first real run on the sample produces a valid review. `risk_agent` stays a stub (Phase 5), and node degradation stays in Phase 7: a failed LLM node still raises.

**Design references:** PLAN.md §3.4 (API surface, diff budget), §5 (models, API behaviours), §8 (`synthesize` input and rubric), §9 (LLM failures, untrusted input), §10 (caching, usage accounting).

**Checked before writing these sub-tasks** (installed `langchain-anthropic` 1.7.10, `anthropic` 2.7.0, and the Claude API reference):
- **Effort:** `ChatAnthropic(reasoning_effort=...)` (alias `effort`) is sent as `output_config.effort`. On Opus 5.5 the default is `medium`, so every node sets it explicitly.
- **Thinking:** it can't be disabled on Opus 5.5 (a 400), so `thinking` is never sent and effort is the only control. `temperature` is never sent either.
- **Structured output:** `with_structured_output(Schema, method="json_schema")` is sent as `output_config.format`. Forced `tool_choice` returns a 400, so `function_calling` is never used.
- **Refusal fallback:** this is the beta `server-side-fallback-2026-07-01` with `fallbacks: "default"`, sent through `client.beta.messages.create`. SDK 2.7.0 accepts `fallbacks`, and `ChatAnthropic(betas=[...])` routes the call to the beta endpoint. **Not yet verified:** that `fallbacks` passes through `model_kwargs`, and that `langchain-anthropic` parses a response containing a `fallback` block (4.1).
- **Caching:** the minimum cacheable prefix on Opus 5.5 is **512 tokens**. Cache reads cost $0.20/MTok and 5-minute writes 1.25× the input price. `usage_metadata.input_token_details` exposes `cache_read` and `cache_creation`.
- **Pricing:** Opus 5.5 costs $4 per MTok of input and $20 per MTok of output. Non-streaming calls use `max_tokens` of about 16K.

- [x] **4.1 Verify-first spike (2–3 small paid calls, est. < $0.05)** ✅ *2 paid calls, actual cost **$0.027***
  - **Results:**
    - **Payload, offline:** as expected. It holds `betas: ["server-side-fallback-2026-07-01"]`, `fallbacks: "default"`, `output_config: {effort, format: {type: "json_schema"}}`, the system prompt's `cache_control` breakpoint, and no `temperature`.
      - *Correction:* `langchain-anthropic` adds `thinking: {"type": "adaptive", "display": "summarized"}` by itself whenever effort is set. That's valid on Opus 5.5 (adaptive is the default; `display` changes visibility, not billing), so we leave it; the summaries will show in Langfuse. The rule stays "never *disable* thinking".
    - **Schema mismatch, offline:** invalid JSON and a wrong shape both give `langchain_core.exceptions.OutputParserException`. With `include_raw=True`, it is **returned** in `parsing_error`, not raised, so `llm.py` raises it, as its own `LLMSchemaError` (see 4.3), which `RetryPolicy` retries.
    - **Refusal, by reading the code:** `response_metadata["stop_reason"] == "refusal"` (plus `stop_details`). Its content wouldn't parse, so it would show up as an `OutputParserException` and be **retried**. `llm.py` therefore checks `stop_reason` **before** the parse result and raises `LLMRefusal` instead, which is never retried (§9: refusals fail fast).
    - **Real calls (2 identical, effort `low`):**
      - Both returned a parsed `ArchitectureFindings` with `stop_reason: end_turn`.
      - The beta and `fallbacks` were accepted (no 400), and `ls_structured_output_format` isn't sent.
      - Caching works: the system prompt (`project_context.md` + instructions = **3,576 tokens**, more than D1's estimate of about 1.5K) was written on call 1 and read on call 2. Cost per call: $0.022 → $0.005.
    - **Usage gotcha:** `usage_metadata.input_token_details.cache_creation` was **0** on the write. The write appears only under `ephemeral_5m_input_tokens`. So the cost uses the raw `response_metadata["usage"]` fields (`input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens`), not `usage_metadata`.
  - **Not verified:** how a *successful* server-side fallback looks (a `fallback` content block next to the text). No refusal happened, and one can't be triggered on purpose. It's covered defensively: `llm.py` reports `response_metadata["model"]` in the usage line, so a fallback model shows up in the trace.
  1. **Offline, no cost:** build the request payload and confirm it contains `output_config.effort`, `output_config.format`, the beta and `fallbacks`, and no `thinking` or `temperature`.
  2. **Offline, no cost:** feed the structured-output parser bad JSON and record the **exact exception type**, which becomes `RetryPolicy(retry_on=...)` in 4.7.
  3. **One real call:** check that the parsed Pydantic object comes back with `usage_metadata`.
  4. **A second identical call:** check `cache_read > 0`.
  5. **Refusal:** read the code path for how `stop_reason: "refusal"` surfaces. A refusal can't be triggered on purpose, so this is a reading check, not a test.
  - *Done when:* the results are written here, and any API assumption that turns out wrong is reported before code depends on it.

- [x] **4.2 `config.py`: the LLM constants** ✅
  - `MODEL_MAIN = "claude-opus-5-5"`, overridable by the `MODEL_MAIN` env var (§5). *Decision:* `llm.py` reads the override at call time, not `config.py` at import time. `dev_run` imports `config` before it loads `.env`, so an import-time read would ignore an override set in `.env`.
  - `EFFORT_ARCHITECTURE = "medium"`, `EFFORT_SYNTH_SIMPLE = "low"` and `EFFORT_SYNTH_COMPLEX = "medium"` (§5). *Changed:* named constants instead of a dict, matching the rest of `config.py`. The risk agent's effort is added in Phase 5.
  - `LLM_MAX_TOKENS = 16_000`, `LLM_MAX_RETRIES = 3` (§9), `SCHEMA_RETRY_ATTEMPTS = 2` (§9), and the fallback beta name.
  - `SYNTH_SIMPLE_DIFF_BUDGET_TOKENS` (20K at first, **100K** after the 4.8 budget decision) and `SYNTH_MAX_FINDINGS = 15` (§3.4, §8).
  - Prices for the cost estimate: input, output, cache read and 5-minute cache write. They are labelled as an estimate.

- [x] **4.3 `llm.py`: the LLM boundary (the only module that imports `langchain_anthropic`)** ✅
  - *Your requirement (added at 4.3):* the graph depends on our own provider-independent wrapper, not on Anthropic objects. It's kept minimal: one public function and our own types, with no provider registry, `Protocol` or base class. Adding a provider later changes only `llm.py`'s private section.
  - **Public (all the nodes see):**
    - `structured_call(schema, *, effort, system, user) -> tuple[schema instance, Usage]`, where `Effort = Literal["low", "medium", "high"]`,
    - `Usage`: the answering model, uncached input, cache reads and writes, output, the estimated cost, and `trace_line(label)`, e.g. `LLM synthesize: 3,650 tokens in (3,576 cached), 197 out, ~$0.005 (claude-opus-5-5).`,
    - errors: `LLMError` (not retried, e.g. the answer hit `max_tokens`), `LLMRefusal` (never retried) and `LLMSchemaError` (the only error `RetryPolicy` retries).
  - **Private (Claude):**
    - `ChatAnthropic` with the effort, `max_tokens`, SDK retries, the refusal-fallback beta and `fallbacks: "default"`, built per call,
    - the system prompt with a `cache_control` breakpoint, and `with_structured_output(method="json_schema", include_raw=True)`,
    - `MODEL_MAIN` read from the env at call time,
    - cost from the API's own usage fields (see 4.1).
  - **Check order in `_interpret`:** refusal, then `max_tokens`, then the parse result. A refused or cut-off answer also fails to parse, but retrying would only repeat it.
  - *Changed from the plan:* `RetryPolicy` (4.7) retries our `LLMSchemaError`, not LangChain's `OutputParserException`, so the graph doesn't depend on LangChain's parser. PLAN.md §9 and §13 are updated.
  - **Test:** `test_llm_response_handling` (4 cases: valid, refusal, `max_tokens`, schema mismatch).
    - It asserts the exact error class, and that the cost estimate reproduces the real 4.1 call ($0.0221).
    - Fault injection: checking the parse result before the refusal makes it fail.
    - The real model's payload was rechecked offline and matches 4.1.

- [x] **4.4 `preprocessing.py`: the API-surface extraction and diff selection (pure, deterministic)** ✅
  - `api_surface(pr, plan) -> list[str]` covers the non-filtered Python files, in `ranked_files` order.
    - It looks at added and removed lines (reusing `_hunks`) for **top-level** `def`, `async def` and `class` at column 0, so methods and nested functions are skipped. It also looks at public module-level assignments (`NAME = …`, `NAME: T = …`, plus `__all__`), but not comparisons (`==`), augmented assignments (`+=`) or `_private` names.
    - Each (kind, name) gives one entry: `added`, `removed`, or `changed` (removed and added, typically a new signature), e.g. `src/flask/rate_limiter.py: added class MemoryRateLimiter`.
    - Other languages are skipped. Because it works line by line, a `def` at column 0 inside a multi-line string is a rare false positive we accept.
    - *Changed:* it takes `plan` instead of `classifications`, because `ranked_files` already excludes mechanical files and gives the order.
  - `select_diffs(pr, plan, budget_tokens) -> (included, cut)` takes whole diffs in `ranked_files` order and never cuts a file mid-diff.
    - *Decision:* a file that doesn't fit is **skipped, and later, smaller files may still use the room left**, which covers more files within the same budget. The cut files are named in the prompt, and they make the review "truncated" (4.8).
  - **On the sample:** the surface is `__all__` and `MemoryRateLimiter` in `rate_limiter.py`, plus the two new test functions. `app.py` adds none, correctly, since its changes are methods inside `class Flask`. All 3 diffs fit the budget (about 1.6K tokens; the budget was 40K at the time, 150K now).
  - **Test:** `test_llm_inputs_api_surface_and_diff_selection` covers:
    - a method skipped, a changed signature, `async def`, an annotated name, `__all__`,
    - a private name skipped, a comparison skipped, a removed name, a context line ignored,
    - a non-Python file skipped, and a mechanical file excluded,
    - the budget: an exact fit, skip-and-continue, and nothing fitting.

- [x] **4.5 `prompts.py`: every prompt, static content first** ✅
  - **System prompts:** static, with no PR content, dates or run values, so they cache.
    - `architecture_system()`: the role, what counts as architectural impact, the impact levels, and the rules on files and the budget. Then the shared evidence rule (every finding quotes an exact diff excerpt, general-knowledge claims start with "Assumption:", and what can't be judged becomes "verify …"), the shared data rule, and **`project_context.md` in a `<project_context>` block (D1)**. About 2.6K tokens.
    - `SYNTHESIZE_SYSTEM`: the §8 risk rubric and rules (secret outside tests → high, disagreements stated, focus areas 1–6 on changed files only, confidence and what limits it), what `<validator_feedback>` means, and the same two shared rules. About 620 tokens by a 4-chars-per-token estimate, just over the 512 minimum; 4.10 checks that it caches.
  - **User messages:**
    - `architecture_user`: the overview, the API surface, and the selected diffs.
    - `synthesize_simple_user`: the overview, the scan hits, the diffs, and on a retry the previous draft and the validator feedback.
    - `synthesize_complex_user`: the overview, the scan hits, and both analysts' findings (highest severity first, at most 15, then counts of the rest, risk findings marked evidence verified or not verified), never raw diffs; the same retry blocks.
  - **The overview** lists the title, author, description, the files in priority order with their flags, the mechanical files skipped, the stats and `must_review`.
  - **Untrusted text:** all PR text, file paths included, is inside data blocks (`pr_overview`, `pr_diff`, `omitted_diffs`, `api_surface`, `scan_hits`, `analyst_findings`, `previous_draft`).
    - Any of our tag names inside it, opening or closing and in any case, is escaped (`<pr_diff` → `&lt;pr_diff`). Nothing else changes, so quoted evidence still matches the diff.
    - Paths in attributes are HTML-escaped.
    - Scan hits are described by rule name and location only, never the matched text.
  - *Changed:*
    - `router._describe_hit` is now public (`describe_hit`), so the trace and the prompt describe hits the same way.
    - The builders take `stats` (scan hits and the stats line come from it).
    - Cut files go in an `<omitted_diffs>` block rather than plain text, because paths are untrusted too.
  - **Test:** `test_prompt_data_blocks`:
    - a hostile title, description and diff (closing tags, a fake opening tag in another case, fake scan hits) can't open or close a block, and ordinary markup in a diff stays unchanged,
    - the system prompts contain no PR text,
    - the complex path shows 15 of 17 findings, highest severity first, with the count of the rest and no raw diffs.
    - Fault injection: removing the escaping makes it fail.

- [x] **4.6 `nodes.py`: the two real LLM nodes** ✅
  - `architecture_analyst`: `select_diffs` within `plan.architecture_diff_budget_tokens`, then `llm.structured_call(ArchitectureFindings, effort=EFFORT_ARCHITECTURE, …)`. Trace: the impact and finding count, any files over the budget, the usage line.
  - `synthesize`:
    - **simple path:** diffs within `SYNTH_SIMPLE_DIFF_BUDGET_TOKENS`, at effort `low`,
    - **complex path:** the analysts' findings, at effort `medium`,
    - **a revision:** passes the previous draft plus `validation_errors` (the `revision_count` logic is unchanged).
    - Trace: the draft's risk, the path and effort, the usage line.
  - Removed: `STUB_CONFIDENCE`, both Phase 4 stub bodies, and the now-unused `fallback_review` import (the function stays for Phase 7). `risk_agent` keeps its stub.
  - Nodes reach the model only through `llm.structured_call` (looked up on the module), so tests patch that one function.
  - **Schema constraints, checked offline:** the SDK's `transform_schema` moves constraints that structured output doesn't support into the field description (`review_focus_areas`: `minItems: 1` kept, `{maxItems: 6}` in the description; `confidence`: `{maximum: 1, minimum: 0}`). Our Pydantic model still enforces them, so a violation is an `LLMSchemaError` (retried once, 4.7). The prompt also asks for 1–6 focus areas. *Superseded by D4 (review pass before 4.10): `ReviewDraft` no longer has these limits, and `finalize` enforces them.*
  - **Tests** (the 4.9 fake was brought forward, because the existing graph tests would otherwise call the API):
    - An **autouse** fixture `llm_calls` replaces `llm.structured_call` in every test with a stateless fake that records each call (schema, effort, prompts). No test can reach the network.
    - `test_graph_paths` now also checks each run's calls: an architecture call only on the complex path, at `medium` and with the diffs and API surface; synthesize at `low` (simple) or `medium` (complex); `<validator_feedback>` only on the retry; raw diffs only on the simple path; the shared system prompt; one usage line per synthesize call.
    - Fault injection: the wrong effort, a retry without feedback, and an architecture prompt without diffs each make it fail.

- [x] **4.7 `graph.py`: `RetryPolicy` on the two LLM nodes** ✅
  - `SCHEMA_RETRY = RetryPolicy(max_attempts=SCHEMA_RETRY_ATTEMPTS, retry_on=llm.LLMSchemaError)` on `architecture_analyst` and `synthesize` (`LLM_NODES`). LangGraph's defaults are kept for the backoff (0.5 s, ×2, jitter).
  - **Why `retry_on` must be explicit:** LangGraph's `default_retry_on` retries every exception except a fixed list of programming errors (`ValueError`, `TypeError`, …). It would therefore retry our `LLMRefusal` and `LLMError`, but §9 says refusals fail fast. Matching is by `isinstance`, so only `LLMSchemaError` is retried.
  - Transient API errors stay with the SDK (`max_retries=3`), so the two retry layers never stack on the same failure.
  - **Test:** `test_llm_retry_policy` (4 cases on the simple path, using the real policy object):
    - a schema mismatch once: 2 attempts, then a review,
    - a schema mismatch every time: 2 attempts, then `LLMSchemaError`,
    - a refusal: 1 attempt, then `LLMRefusal`,
    - another `LLMError`: 1 attempt.
    - Fault injection: the default `retry_on` (refusals and errors retried) and no policy at all each make 2 cases fail. The backoff adds about 2 s to the suite.

- [x] **Budget decision (before 4.8): raise the diff budgets** ✅
  - **Your question:** Opus 5.5 has a 1M context, so are 40K (architecture) and 20K (simple path) based on cost, latency or quality, or just conservative?
  - **Answer:** conservative judgment calls, not measured.
    - They're caps, not spend: a PR below the cap costs the same at any cap.
    - **20K mattered only for big docs-only PRs.** Simple code PRs are ≤ 50 lines by the routing rules, so the cap could drop whole doc files from a docs-only review.
    - **40K mattered only for very large PRs.** Even then, cut files are named, they cap confidence, the API surface still covers every Python file, and the risk agent reads any diff on demand.
  - **Decision (yours):**
    - `ARCH_DIFF_BUDGET_TOKENS = 150_000`: about 600K characters, about 15K diff lines; worst case about $0.60 of uncached input per call.
    - `SYNTH_SIMPLE_DIFF_BUDGET_TOKENS = 100_000`.
    - Both are documented in `config.py` and PLAN.md §3.2 and §8 as **initial values, not empirically validated**. Omitted-file reporting and the confidence cap stay.
    - Tuning them needs paid runs on large PRs, which wait for your approval.

- [x] **4.8 `finalize`: truncation from the diff budgets (PLAN.md §8, "Truncated diffs: budget or malformed")** ✅
  - `confidence_signals(..., diffs_cut=...)`: `truncated` is true for a malformed or missing diff **or** any file the route's diff budget left out.
  - **D2 (approved):** `finalize` recomputes the cut files with the same pure `select_diffs` call; there's no new state field and the graph is unchanged.
  - `nodes._diff_budget(state)` is the single definition of the budget for each path (architecture budget on complex, simple budget on simple). The two nodes and `finalize` all use it, so they can't disagree.
  - The cap stays `CONF_CAP_TRUNCATED = 0.7`, traced as "confidence capped at 0.70: part of the diff wasn't seen".
  - **Test:** `test_diff_budget_truncation`, 3 cases:
    - **simple path over its budget:** `<omitted_diffs>` and no `<pr_diff>` in the synthesize prompt, the node's "over the diff budget, not shown" trace line, the 0.70 cap,
    - **complex path over the architecture budget:** the same, with the stub risk agent replaced by one that "read" every must-review file, so the 0.5 coverage cap doesn't hide the 0.7 truncation cap,
    - **complex path within the real budget:** nothing cut, no cap.
    - Fault injection: `finalize` ignoring the cut files (2 cases fail) and `finalize` using the simple budget on the complex path (1 case fails).

- [x] **4.9 Tests (no API key, no network)** ✅
  - Most of the checklist was done along the way, a test per sub-task:

    | Planned | Where |
    |---|---|
    | `api_surface` and `select_diffs` | `test_llm_inputs_api_surface_and_diff_selection` (4.4) |
    | Data blocks the PR can't escape | `test_prompt_data_blocks` (4.5) |
    | The graph on a fake `structured_call` (effort per path, retry input) | the autouse `llm_calls` fixture + `test_graph_paths` (4.6) |
    | `RetryPolicy` | `test_llm_retry_policy` (4.7) |
    | Truncation from the budgets | `test_diff_budget_truncation` (4.8) |
    | Response → result or error, cost | `test_llm_response_handling` (4.3) |

  - **Gap found and closed:** `structured_call` itself never ran in a test, since the fake replaces it everywhere, so nothing guarded the real request 4.1 verified.
    - New `test_llm_request_payload` builds the real payload offline (placeholder key, nothing sent) and checks: model, `max_tokens`, effort, the fallback beta and `fallbacks`, the system prompt's cache breakpoint, adaptive thinking, no `temperature` or `tool_choice`, and the `MODEL_MAIN` env override at call time.
    - To test it, the message construction moved into `llm._messages`.
    - Fault injection: dropping the cache breakpoint, or ignoring the env override, each makes it fail.
  - **The 4.0 note on running the graph twice: closed, unchanged.**
    - `test_graph_paths` still runs `stream` (the node order) and `invoke` (the output schema hides the internal state, which `stream`'s values mode doesn't show).
    - The fake is stateless, so both runs see the same answers. The call assertions use the second run's half of the recorded calls.
  - **Checks:**
    - `uv run pytest -q` passes (29), also with `ANTHROPIC_API_KEY` and the Langfuse keys unset.
    - The autouse fake means no test can reach the network.
    - The suite takes about 2.5 s; the two retry cases (real backoff) account for about 1.7 s.

- [x] **Review pass before 4.10 (code-review + code-simplifier on the Phase 4 diff)** ✅
  - 28 findings (10 code-review, 18 code-simplifier; one reported by both). Each was checked against the code, and you approved the list.
  - **Applied, prompts:**
    - **P1 (both tools):** the synthesizer had the architecture analyst's evidence rule ("each finding cites `evidence`… ground every claim in the diff"), but it writes no findings and sees no diff on the complex path. It now has its own rule: ground claims in the diff or in the analysts' quoted evidence, treat unverified evidence as weaker.
    - **P2:** "You always get the PR's facts, plus either the diff or the findings, never both."
    - **P3:** the focus-area count is built from `config.MAX_FOCUS_AREAS`.
    - **P4:** the data rule is re-wrapped.
  - **Applied, API surface:**
    - **A1:** Python keywords (`else: y = 2`) and prose after a colon (`Note: see https://x?a=1`) are no longer reported as names; annotations may hold only type-like characters.
    - **A2:** private `def _x` and `class _X` are skipped, the same rule as for assignments.
    - **A3:** the suffix check ignores case (`.PY`).
    - **A4:** changes are matched by name, so `def` → `async def` is "changed", and an identical definition line on both sides (a move) is left out.
  - **Applied, B1:** `_block` strips only the outer newlines, so a diff's own whitespace is kept and quoted evidence still matches.
  - **Applied, D4 (yours):** `ReviewDraft` keeps only "at least 1 focus area", which the API enforces. The 6-item maximum and the 0–1 confidence range would reach the model only as hints, so a slip would fail the parse, be retried with the same prompt, and end the run. `finalize` now enforces both (it cuts with a trace line, and clamps), and `ReviewOutput` keeps every limit. PLAN.md §8 is updated.
  - **Applied, cleanups:** plain `if`s instead of nested conditionals; one loop instead of a generator reusing `line`; `_surface_item`; a PEP 8 blank line; plain locals in `_usage`; the provider named in the docstring; `config.LLM_MAX_RETRIES` named in `graph.py`; the "raised from" history moved out of `config.py` (still "INITIAL VALUES, not empirically validated"); the test docstring as bullets with no count; `synth_runs`; a `nonlocal` counter; `reverse=True` in the sort.
  - **Tests:** the API-surface test gains keyword, prose, private, `async`, moved and `.PY` cases; the data-block test gains a trailing-whitespace case. New `test_draft_limits_enforced_in_finalize`: 7 focus areas and confidence 1.3 parse, the run completes with 6 areas, a "cut by the limit" line and capped confidence, and no retry.
    - Fault injection: not skipping keywords, reporting moves, a full `.strip()`, and reverting D4 each make a test fail.
  - **Not changed, with reasons:**
    - **Cost at Opus prices when a fallback model answers:** fallbacks are rare and unverified (4.1), and the usage line names the model.
    - **A new client per call:** the setup is small next to a multi-second call, the Phase 5 agent keeps one instance for its loop, and a cache would add global state.
    - **The simple budget on `ReviewPlan`:** `_diff_budget` is already the single source, and D2 was approved.
    - **The double guard on the previous draft**, and `.get` vs `[]`: harmless.

- [x] **4.10 First real run on the sample** ✅ *Option A (yours): one paid run, not two. Actual cost **≈ $0.100** (estimate $0.05–0.10).*
  - **Result:** `uv run python -m agent_workflow.dev_run` exited 0 in about 30 s, with Langfuse tracing on.
    - **The output is valid:** risk `high`, complexity 5.5, 6 focus areas, all on changed files.
    - **Validation passed first time**, so there was no retry.
    - **Confidence:** the model said 0.80; finalize gave 0.50 (−0.05 for the core file, then the coverage cap, because the stub risk agent read nothing). That is as designed.
  - **Calls**, both `claude-opus-5-5` and costed from the API's usage fields:

    | Call | Effort | Input (of which cache written) | Output | Cost |
    |---|---|---|---|---|
    | `architecture_analyst` | medium | 7,412 (4,323) | 1,320 | $0.060 |
    | `synthesize` | medium | 3,323 (1,621) | 1,244 | $0.040 |

  - **Verified, the three reasons for running now:**
    1. **Structured output with thinking at `medium`** parses, so `include_raw` and the JSON-schema parse work next to thinking.
    2. **`ReviewDraft` is accepted by the real API**, with the transformed schema and D4.
    3. **The prompts give a grounded review:**
       - the architecture analyst quoted exact diff lines (`self._enforce_rate_limit(ctx)`, `return req.access_route[0]`, `__all__ = ("MemoryRateLimiter",)`),
       - it applied the project conventions by name (scope, forwarded headers, config docs, test fixtures),
       - it labelled one point "Assumption:" (the Flask-Limiter key clash).
  - Both system prompts were **written to the cache**: architecture 4,323 tokens, synthesize 1,621. Reading them back needs a second run, so it moves to Phase 5.
  - **Langfuse:** one `pr-review` trace; every node nested; each Claude call is a generation under its node, with the model and usage.
  - **Problems found:**
    - **F1, misattribution (prompt).** Every finding came from the architecture analyst, and the stub risk reviewer had none ("Risk reviewer: completed."). Yet the draft says "Both analysts agree" and "The risk reviewer has three high findings". The findings block doesn't say how many findings each analyst reported. This can recur in Phase 5 whenever the risk agent finds nothing.
    - **F2, doubled generations in Langfuse (dev only).** Each call appears twice: `ChatAnthropic` (LangChain) and `anthropic.messages.create`, the Anthropic SDK's own tracing span (scope `com.anthropic.sdk.python`). Langfuse's cost totals per trace are therefore about 2×. Our own trace line is correct. Langfuse has a `should_export_span` hook to drop that scope.
  - **F1, fixed (offline, no paid re-run):**
    - The findings block now states each analyst's own count, e.g. "Architecture analyst: impact high, 6 finding(s). …" and "Risk reviewer: completed, 0 finding(s).".
    - `SYNTHESIZE_SYSTEM` adds a rule: each finding is labelled with its analyst; attribute it only to that analyst; say the analysts agree only on a point both reported; 0 findings means nothing to report.
    - `test_prompt_data_blocks` checks the counts, the label and the rule. Reverting the count makes it fail.
    - Whether the model now attributes correctly is checked on the next paid run (Phase 5).
  - **F2, deferred to Phase 5 (your decision):** still open as described above. *To do in Phase 5:* filter scope `com.anthropic.sdk.python` through Langfuse's `should_export_span`, after verifying that a custom filter doesn't drop Langfuse's default spans, then check one trace has one generation per call.
  - **Moved to Phase 5:** the second run (cache reads), the Langfuse walkthrough, judging review quality with a real risk agent, and checking the F1 fix on a real run.

- [x] **4.11 Docs and final review pass** ✅
  - **F1 (from 4.10), fixed before the pass.** See 4.10.
  - **Final pass:** code-review (level `high`) and code-simplifier (report-only) on **all** Phase 4 changes, including the new `llm.py` and `prompts.py`. 18 findings (10 code-review, 8 code-simplifier). Each was checked against the code, and you approved the list.
  - **Applied, code review:**
    - **R1, a side effect of D4:** an out-of-range model confidence (e.g. `85`) had the penalties subtracted from it, then the final 0.95 cap hid them, giving the highest possible confidence.
      - *Scale checked first:* the prompt says "confidence (0 to 1)", the schema note says 0–1, and nothing defines a percentage.
      - So `calibrate_confidence` clamps the model's value to [0, 1] **before** the penalties (it never reinterprets 85 as 85%), with a trace line ("model confidence 85 is outside 0–1; clamped to 1.00 before adjustments").
      - PLAN.md §8 is updated.
    - **R2:** the stub risk agent returned an empty `RiskFindings`, so with the F1 counts the synthesizer read "Risk reviewer: completed, 0 finding(s)", i.e. a review that found nothing, although it never ran. It now returns `risk=None` ("Risk reviewer: not available.") and traces "stub, not run". Coverage and confidence are unchanged, since `None` is already handled.
    - **R3:** the API-surface annotation rule accepted words separated by spaces, so prose like `Note: see this = that` was a "name". An annotation must now look like a type: dotted names, `[...]` subscripts, joined by `|`.
      - **Documented limitation (accepted, rare):** a keyword argument at column 0 inside a call (`a=1,`) can't be told apart from a valid tuple assignment, so it's reported as a name. Formatters indent such lines.
    - **R4:** the prompt's per-file flags are built from `router.STRUCTURAL_FLAGS`, the labels routing and the fallback review use ("core file", "deleted source file", …), plus two flags only the model needs. The old copy had drifted.
    - **R5:** a simple-path revision traced the "over the diff budget" line again; now only the first attempt does.
  - **Applied, code-simplifier (S1–S8):** `strip("\n")` on its own line; the data rule says "report it as a concern" (the synthesizer writes no findings); "diff budget" in the architecture prompt; `kind` picked once in `api_surface`; two duplicated inline comments removed from `ReviewDraft`; a local for the omitted-files list; tests built from `config.MAX_FOCUS_AREAS` and `SYNTH_MAX_FINDINGS`; the 4.6 note above marked as superseded by D4.
  - **Tests:**
    - **New `test_confidence_clamped_before_penalties`:** `85` and `1.3` → clamped to 1.0, then −0.05 and −0.10 → **0.85**; `0.9` → 0.75 with no clamp line; `−0.2` → the 0.1 floor.
    - `test_graph_paths` checks the R4 flag label and that a complex-path prompt says "Risk reviewer: not available." (R2).
    - The API-surface test adds a prose line and an `int | None` annotation (R3).
    - The truncation test adds a simple-path revision whose budget line appears once (R5).
    - The D4 test checks the clamp line.
    - **Fault injection:** undoing each of R1–R5 makes a test fail (R1: 4, R2: 3, R3: 1, R4: 2, R5: 1).
      - *Note:* the first R2 injection replaced the wrong `"risk": None` (the one in `parse_pr`, not the stub) and wrongly looked uncaught. Rerun on the stub, the intended assertion catches it.
  - **Not changed, with reasons:**
    - **Failed calls' usage isn't in the trace:** a retried node returns no state, so the failed attempt can't write a line. Langfuse records it. Known limitation; Phase 7 (node failures) can attach usage to the error.
    - **The escaping of our tag names inside a diff:** the deliberate, documented exception (the reviewer's `&amp;lt;` example doesn't happen, since `&` isn't escaped). *Phase 5 note:* the evidence guards should apply the same escaping to the diff before comparing.
    - **Fallback-model prices, and a new client per call:** decided in the earlier pass.
    - **`finalize` recomputing the cut diffs:** D2.
    - **"PLAN.md edited to justify the implementation":** the edits record decisions you approved (the budgets, D2, D4), as CLAUDE.md asks.
  - **Checks:** `uv run pytest -q` passes (35), `py_compile` is clean, and the graph diagram prints. No paid calls in 4.11.

**Decisions needed before implementing:**
- **D1. Project conventions in the architecture prompt?** PLAN §4 lists `project_context.md` as a context source but doesn't say which node gets it.
  - *Recommended:* put the whole file at the end of `ARCHITECTURE_SYSTEM`. It's static, so it caches, and about 1.5K tokens is above the 512-token minimum. The analyst judges impact *against* the project's architecture.
  - *Alternative:* leave it to the Phase 5 risk agent's `read_project_context` tool only.
- **D2. How `finalize` learns that the budget cut a file.**
  - *Recommended:* recompute it in `finalize` with the same pure `select_diffs` call. It's deterministic, needs no new state field, and the graph stays unchanged.
  - *Alternative:* add a `ReviewState` field, which changes PLAN §6.
- **D3. Paid calls.** Your approval is needed for about 2–3 calls in 4.1 (< $0.05) and 2 sample runs in 4.10 (about $0.10–0.30 each). Cost estimates are from list prices; the real figures will be recorded.

**Out of scope for Phase 4:**
- the risk agent and the hallucination guards (Phase 5), so architecture findings aren't evidence-checked yet,
- Jev (Phase 6),
- degradation on LLM failure, the CLI and exit codes (Phase 7).

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

## Phase 8: Langfuse tracing (dev only) ✅
**Files:** `observability.py`, `dev_run.py`, `langfuse/` (`docker-compose.yml`, `.env.example`).
**Check:** a nested trace appears in the Langfuse UI, and the app still runs without the keys.

*Decision changed (order):* done before Phase 4 instead of after the CLI, at your request, so the LLM phases are traced while they're built. The phase number is kept so existing references stay valid.

- [x] **8.1 Local Langfuse server** — Langfuse's official `docker-compose.yml` (unmodified) in `langfuse/`, with every `CHANGEME` value overridden in `langfuse/.env` (git-ignored; template in `langfuse/.env.example`). Headless init creates the user, org, project and API keys on first start. `TELEMETRY_ENABLED=false`. Kept in the repo at your request (it's dev tooling, not part of the app).
- [x] **8.2 `observability.py`** — `get_langfuse_handler()` returns a `CallbackHandler` only when both keys and `LANGFUSE_BASE_URL` are set and `langfuse` is installed (the base URL was added in the 4.0 review); `traced_run(name, metadata=)` yields the run config (`{}` when off), sets the trace name via `propagate_attributes`, and flushes on exit.
- [x] **8.3 `dev_run.py`** — `uv run python -m agent_workflow.dev_run [pr.json]` runs the graph traced. A stand-in until the Phase 7 CLI, which reuses `traced_run`.
- [x] **8.4 Test** — `test_tracing_is_optional`: no keys, no base URL (added in 4.0), or no package gives no handler and `{}` config, and the graph still runs.
- [x] **8.5 Phase check** — verified via the v2 observations API: one `pr-review` root with each node nested under it (both analysts on the complex path). Keys set with the server down: the run still exits 0 (export errors logged). No keys: tracing off. `uv run --no-dev`: the module imports and returns no handler.
  - *Note:* the server runs Langfuse v4 in events-only mode, so the legacy `GET /api/public/traces` is unavailable; read traces with `GET /api/public/v2/observations` (SDK: `get_client().api.observations.get_many`).

## Phase 9: Deliverables
**Files:** `code_review_output.json`, `requirements.txt`, `README.md`.
**Check:** the README covers the design, trade-offs, the Jev decision and fallback, AI-tool usage, and what was new to you.
*Sub-tasks: to be written when the phase starts.*
