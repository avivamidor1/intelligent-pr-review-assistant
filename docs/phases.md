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
| 4 | LLM foundation | First real run on the sample | — |
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

*Sub-tasks 4.1+: to be written next, for your review.*

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
