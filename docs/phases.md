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
