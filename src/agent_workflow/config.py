"""Deterministic constants: noise filter, categories, priority, routing rules, scoring.

Design: docs/PLAN.md §3.1 (noise filter, categories), §3.2 (priority, review plan),
§3.3 (hard routing rules), §8 (complexity, confidence), §9 (fallback review).

Every number and path pattern used by the deterministic layer is defined here, once.
Regexes that are part of a parsing or scanning algorithm (diff hunks, import lines,
secret and injection rules) live in preprocessing.py, next to the code that uses them.
The model, effort levels and LLM limits are in the LLM section (Phase 4), the risk
agent's limits in their own section (Phase 5), the Jev router's in theirs (Phase 6), and
the CLI's and degradation's in theirs (Phase 7).

The values come from PLAN.md. Most are judgment calls, not measured on real PRs, and the
comments say what each one is meant to achieve. Two ideas run through all of them:
- Mistakes are asymmetric: an over-reviewed simple PR costs cents, an under-reviewed
  risky PR is a missed risk. Doubtful cases therefore lean towards "more review".
- Scores use log scales and caps, so one large or unusual file can't dominate.

Path pattern conventions (applied by preprocessing):
- `*_NAME_GLOBS` are fnmatch globs matched against the file's basename.
- `*_DIR_PATTERNS` end with "/" and match when the path starts with the pattern or
  contains it after a "/" (e.g. "vendor/" matches "vendor/x.js" and "lib/vendor/x.js").
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Noise filter (§3.1)
# Machine-written files: no human logic to review, and their diffs can be huge.
# Sending them to an LLM wastes tokens and buries the real changes.
# ---------------------------------------------------------------------------

NOISE_NAME_GLOBS: Final = ("*.lock", "package-lock.json", "*.min.js", "*_pb2.py")
NOISE_DIR_PATTERNS: Final = ("generated/", "vendor/")

# The two forms git uses for a binary file. Neither has text lines to review.
# Applied per line (re.MULTILINE).
BINARY_MARKER_PATTERNS: Final = (r"^Binary files .+ differ\r?$", r"^GIT binary patch\r?$")

# ---------------------------------------------------------------------------
# File categories (§3.1); checked after mechanical, first match wins
# Common Python-project conventions. Test is checked before docs, so `tests/README.md`
# counts as a test. Anything unmatched is source: a doubtful file is treated as code.
# ---------------------------------------------------------------------------

TEST_NAME_GLOBS: Final = ("test_*.py", "*_test.py", "conftest.py")
TEST_DIR_PATTERNS: Final = ("tests/",)

DOCS_NAME_GLOBS: Final = ("*.md", "*.rst", "*.txt", "CHANGES", "LICENSE")
DOCS_DIR_PATTERNS: Final = ("docs/",)

CONFIG_NAME_GLOBS: Final = ("pyproject.toml", "*.cfg", "*.ini", "*.yaml", "*.yml")
CONFIG_DIR_PATTERNS: Final = (".github/workflows/",)

# Config files that the docs `*.txt` rule would otherwise catch, so they're checked
# before docs. Dependency manifests change what gets installed: they're config, not prose.
CONFIG_BEFORE_DOCS_NAME_GLOBS: Final = ("requirements*.txt",)

# Languages we can reasonably judge from a diff. A source file with any other extension
# is flagged `is_unknown_type` and routes complex (§3.3): if we can't tell what it is,
# we don't let it take the light path.
KNOWN_CODE_EXTENSIONS: Final = frozenset(
    {
        ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt",
        ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh",
        ".sql", ".html", ".css", ".scss",
    }
)  # fmt: skip

# ---------------------------------------------------------------------------
# Priority and the review plan (§3.2)
# ---------------------------------------------------------------------------

# Areas where a bug easily becomes a security or data problem. Matched as case-insensitive
# substrings on code files only (source, config, test), so `docs/config.rst` is not
# sensitive. Over-matching (e.g. "author.py") is accepted: it only makes routing more
# thorough.
SENSITIVE_PATH_SUBSTRINGS: Final = (
    "auth", "security", "session", "crypto", "config", "middleware", "migrations",
)  # fmt: skip

# priority = Σ weight·flag + log10(1 + lines_changed)
# The weights encode an order: core > source > new module > sensitive. The size term is a
# log, so it adds about 1 per 10x lines (10 → 1, 100 → 2, 1000 → 3): a big file climbs
# the ranking, but size alone rarely outranks a structural flag. Tests and docs get a
# negative weight, so they rank below code, but they stay in the review as context.
# Sample PR: app.py 3+2+log10(63) ≈ 6.80, rate_limiter.py 2+1.5+log10(54) ≈ 5.23,
# test_rate_limit.py −1+log10(37) ≈ 0.57.
PRIORITY_WEIGHT_CORE: Final = 3.0
PRIORITY_WEIGHT_SOURCE: Final = 2.0
PRIORITY_WEIGHT_NEW_MODULE: Final = 1.5
PRIORITY_WEIGHT_SENSITIVE: Final = 1.0
PRIORITY_WEIGHT_TEST: Final = -1.0
PRIORITY_WEIGHT_DOCS: Final = -2.0

# The top-N files the risk agent must read, besides the flagged ones. 5 reads fit inside
# the smallest tool budget (8), with 3 calls left for searches.
MUST_REVIEW_TOP_N: Final = 5

# Diff budgets for the two LLM calls that see raw diffs, filled with whole files in priority
# order. INITIAL VALUES, not empirically validated: no cost, latency or quality measurement
# on large PRs backs them yet. They're caps, not spend (a PR below them costs the same at
# any cap), and they exist for focus and cost, not to fit the 1M context (§5). Whatever
# doesn't fit is named in the prompt and the trace, and caps confidence as truncated (§8).
#
# Architecture analyst: 150K tokens ≈ 600K characters ≈ 15K diff lines, so only very large
# PRs are cut; worst case ≈ $0.60 of uncached input per call at list price. What's cut is
# still covered by the API surface (all Python files) and by the risk agent, which reads
# any diff on demand (§3.4). Chosen in Phase 4 (docs/phases.md, budget decision).
ARCH_DIFF_BUDGET_TOKENS: Final = 150_000

# Simple-path synthesizer (§8): simple code PRs are ≤ 50 changed lines (routing rules), so
# only a large docs-only PR can reach this. 100K keeps such docs in the review, at low
# effort. Chosen in Phase 4 (docs/phases.md, budget decision).
SYNTH_SIMPLE_DIFF_BUDGET_TOKENS: Final = 100_000

# The complex-path synthesizer gets at most this many findings, highest severity first, plus
# counts of the rest (§3.4): enough to judge the PR without burying the strongest evidence.
SYNTH_MAX_FINDINGS: Final = 15

# Risk-agent tool budget: clamp(BASE + PER_CODE_FILE × code_files, MIN, MAX).
# BASE: room for a few reads and searches, even on a tiny PR.
# PER_CODE_FILE: about one diff read plus one follow-up search per code file.
# MAX: caps cost and latency on huge PRs. There, the agent reads `must_review` first and
#   the coverage check reports what it skipped (§3.4).
# MIN equals BASE, so it never binds today. It's kept so the code matches the plan's formula.
TOOL_BUDGET_BASE: Final = 8
TOOL_BUDGET_PER_CODE_FILE: Final = 2
TOOL_BUDGET_MIN: Final = 8
TOOL_BUDGET_MAX: Final = 30

# ---------------------------------------------------------------------------
# Hard routing rules (§3.3); above either limit routes complex
# What "a small change" means. Above these limits, rules can't call a PR trivial, and
# a full review costs little compared with a missed risk. At or below them (with nothing
# core, sensitive or new), the PR is the "uncertain middle" that Jev decides (the Jev
# router section below).
# ---------------------------------------------------------------------------

MAX_CODE_FILES: Final = 3
MAX_CODE_LINES: Final = 50

# A route reason names at most this many files per rule, then "+N more", so a 60-file
# PR still gives a one-line trace entry.
ROUTE_REASON_MAX_PATHS: Final = 5

# Focus areas in a review (draft and final output). Few enough for a reviewer to act on.
MAX_FOCUS_AREAS: Final = 6

# validate → synthesize (§8): at most this many revisions of a draft that fails the checks.
# One retry fixes most slips; a second rarely helps and doubles the cost of a bad case.
MAX_VALIDATION_RETRIES: Final = 1

# Split-PR advice (§3.4): finalize recommends splitting a PR that is both large and spread
# out, i.e. MORE than this many non-filtered source files AND MORE than this many modules
# (distinct parent folders). Like MAX_CODE_FILES, up to the limit is fine. "And", not "or":
# a small PR across many folders (e.g. a rename across packages) is usually fine as one PR.
# Advice only; it never changes route or risk.
SPLIT_PR_MAX_SOURCE_FILES: Final = 40
SPLIT_PR_MAX_MODULES: Final = 6

# ---------------------------------------------------------------------------
# Complexity score (§8)
# clamp(BASE + size + spread + core + new_modules, MIN, MAX), rounded half-up to STEP
# The term maxima add up to the scale: 1 + 3 + 2 + 3 + 1 = 10. Size and structure (spread,
# core, new modules) weigh about the same, so a big but simple change can't hit 10 on
# its own. Rounding to 0.5 avoids false precision. The score measures structure, not
# subtlety. Sample PR: 1 + 2.06 + 0.22 + 1.5 + 0.5 ≈ 5.29 → 5.5.
# ---------------------------------------------------------------------------

COMPLEXITY_BASE: Final = 1.0  # the floor: a one-line docs typo scores 1.0
COMPLEXITY_MIN: Final = 1.0
COMPLEXITY_MAX: Final = 10.0
COMPLEXITY_ROUND_STEP: Final = 0.5

# size = SIZE_MAX · log10(1 + L) / log10(1 + SIZE_SATURATION_LINES), clamped to [0, SIZE_MAX]
# A log scale: 10 lines → 1.0, 100 → 2.0, 1000+ → 3.0. The first 100 lines say more about
# complexity than the next 900.
COMPLEXITY_SIZE_MAX: Final = 3.0
COMPLEXITY_SIZE_SATURATION_LINES: Final = 1000

# spread = SPREAD_MAX · (n − 1) / (SPREAD_SATURATION_FILES − 1), clamped to [0, SPREAD_MAX]
# A single source file adds 0. The term grows linearly up to 10 files and stays at the max
# beyond that.
COMPLEXITY_SPREAD_MAX: Final = 2.0
COMPLEXITY_SPREAD_SATURATION_FILES: Final = 10

# Core files carry the most weight, because their callers are outside the diff.
# Two core files reach the max.
COMPLEXITY_CORE_PER_FILE: Final = 1.5
COMPLEXITY_CORE_MAX: Final = 3.0

# A new module adds design surface, but nothing depends on it yet. Two reach the max.
COMPLEXITY_NEW_MODULE_PER_FILE: Final = 0.5
COMPLEXITY_NEW_MODULE_MAX: Final = 1.0

# ---------------------------------------------------------------------------
# Confidence calibration (§8): subtract penalties, then apply caps with min(), then clamp
# The LLM's own confidence is the starting point, but it can't see what the system knows
# it missed. Penalties are small deductions for soft signals that add up. Caps are hard
# ceilings for structural gaps, where no self-assessment should be trusted above a point.
# ---------------------------------------------------------------------------

# Penalties (subtracted)
CONF_PENALTY_DISAGREE_ONE_LEVEL: Final = 0.1  # analysts one level apart: mild tension
CONF_PENALTY_VALIDATION_RETRY: Final = 0.05  # the first draft was inconsistent, then fixed
CONF_PENALTY_PER_CORE_FILE: Final = 0.05  # each core file has callers we never saw
CONF_PENALTY_CORE_FILES_MAX: Final = 0.1
CONF_PENALTY_PER_UNVERIFIED_FINDING: Final = 0.05  # findings the agent couldn't confirm
CONF_PENALTY_UNVERIFIED_MAX: Final = 0.15
CONF_PENALTY_INJECTION: Final = 0.1  # injection-like text in the PR: the model may have been steered

# Caps (upper bounds)
# Coverage: with no must-review file read, the cap is 0.5. With all of them read, it is 1.0,
# so the cap doesn't bind.
CONF_CAP_COVERAGE_BASE: Final = 0.5  # cap = BASE + SLOPE · must_review_coverage
CONF_CAP_COVERAGE_SLOPE: Final = 0.5
CONF_CAP_TRUNCATED: Final = 0.7  # part of the diff wasn't seen (budget or malformed)
CONF_CAP_DISAGREE_TWO_LEVELS: Final = 0.6  # one analyst says low, the other high
CONF_CAP_AGENT_HIT_LIMIT: Final = 0.6  # the agent wanted to investigate more than allowed
CONF_CAP_VALIDATION_STILL_FAILING: Final = 0.5  # the draft is inconsistent even after a retry
CONF_CAP_ARCHITECTURE_DEGRADED: Final = 0.5  # the architecture analyst failed
# The risk agent failed. This cap is lower than the architecture one, because the risk
# agent is the main evidence for `risk_level`.
CONF_CAP_RISK_DEGRADED: Final = 0.4

# Final clamp. Never 1.0: the system has no repo access and doesn't run tests (§4).
# Never 0: every output is still based on the actual diff.
CONF_MIN: Final = 0.1
CONF_MAX: Final = 0.95

# Deterministic fallback review (§9): rule-based facts only, with no reasoning about
# behaviour. It sits just above the floor, because its facts are still correct.
FALLBACK_CONFIDENCE: Final = 0.2

# ---------------------------------------------------------------------------
# LLM (§5, §9, §10). Values checked against the installed SDKs in Phase 4.1.
# ---------------------------------------------------------------------------

# One Claude model for every node: one prompt cache and one set of behaviours (§5).
# The MODEL_MAIN environment variable overrides it; llm.py reads it at call time, after
# .env is loaded, so the override works from .env too.
MODEL_MAIN: Final = "claude-opus-5-5"

# Effort per call (§5). Thinking can't be disabled on Opus 5.5 and its default effort is
# "medium", so every call sets one explicitly.
EFFORT_ARCHITECTURE: Final = "medium"  # one focused judgment call
EFFORT_SYNTH_SIMPLE: Final = "low"  # a small diff that needs a short review
EFFORT_SYNTH_COMPLEX: Final = "medium"  # merges findings that already exist
EFFORT_RISK_AGENT: Final = "high"  # multi-step tool use and the hardest reasoning (also its recovery call)

# Per call. Thinking counts toward it, so not lower; non-streaming calls stay under the
# SDK's HTTP timeout at this size (§5).
LLM_MAX_TOKENS: Final = 16_000
# Transient errors (429, 5xx, timeouts, connection) are retried by the Anthropic SDK with
# exponential backoff (§9).
LLM_MAX_RETRIES: Final = 3
# A schema mismatch is retried by the node's RetryPolicy: 2 attempts = 1 retry (§9).
SCHEMA_RETRY_ATTEMPTS: Final = 2

# Server-side refusal fallback (§5): on a refusal, the API re-runs the request on a fallback
# model it picks by refusal category ("default"), inside the same call.
FALLBACK_BETA: Final = "server-side-fallback-2026-07-01"
FALLBACK_MODE: Final = "default"

# List prices for claude-opus-5-5, $ per million tokens (checked 2026-11). Used only for the
# cost estimate in the trace, never for billing. A 5-minute cache write is 1.25× input.
PRICE_INPUT_PER_MTOK: Final = 4.00
PRICE_OUTPUT_PER_MTOK: Final = 20.00
PRICE_CACHE_READ_PER_MTOK: Final = 0.20
PRICE_CACHE_WRITE_PER_MTOK: Final = 5.00

# ---------------------------------------------------------------------------
# Risk agent (§7). Its tool budget comes from the review plan (TOOL_BUDGET_* above).
# ---------------------------------------------------------------------------

# max_model_calls = max_tool_calls + MODEL_CALL_SLACK. Every non-final turn uses at least
# one tool call, so the tool budget is the real cost limit, and this is only a backstop:
# one call to answer after the last tool result, plus one for a turn whose tools were all
# blocked. A tighter value would end normal one-tool-per-turn runs before the tool budget
# is spent, with no answer.
MODEL_CALL_SLACK: Final = 2

# LangGraph steps one model→tools turn takes in the agent with our middleware (measured
# in Phase 5.1: k tool turns then an answer need 5k + 5 steps; a run the model-call limit
# ends after m calls needs 5m + 2, the worst case). recursion_limit =
# RISK_AGENT_STEPS_PER_TURN × (max_model_calls + 1), passed in the run's config because
# create_agent defaults to 9,999. It should never trigger: the two call limits stop the run first.
RISK_AGENT_STEPS_PER_TURN: Final = 5

# Tool output bounds (§3.5, §7). Every tool is a bounded read over the PR.
# Every tool result (error messages included) is at most this many characters. The tools
# stay under it by design; a final cut enforces it for pathological input (a huge path).
TOOL_RESULT_MAX_CHARS: Final = 20_000
# The agent's executed tool output in one run is then at most TOOL_BUDGET_MAX ×
# TOOL_RESULT_MAX_CHARS characters (~150–200K tokens, well inside the model's 1M window);
# a test keeps the product at or under this ceiling. Calls the budget blocks get a short
# fixed message from the middleware.
TOOL_OUTPUT_TOTAL_MAX_CHARS: Final = 600_000
# A read_file_diff page ends after this many lines, or before its text (page 1: with the
# hunk-header list) would pass TOOL_PAGE_MAX_CHARS, whichever comes first. Every line is on
# some page. Normal code (~40 characters a line) fills the line limit first.
TOOL_DIFF_PAGE_LINES: Final = 400
TOOL_PAGE_MAX_CHARS: Final = 18_000
SEARCH_DIFF_MAX_HITS: Final = 50  # search_diff stops here and says the rest were cut
# One hit line is cut to this length: a minified or generated line could otherwise make a
# single search result enormous.
SEARCH_HIT_MAX_CHARS: Final = 200
# Each line read_file_diff shows is cut to this length, with the number of characters cut,
# so one minified line can't put megabytes into a page. Normal code lines are far shorter.
TOOL_LINE_MAX_CHARS: Final = 500
# Page 1 of read_file_diff lists at most this many hunk headers, with the page each is on
# (then "+N more"), each cut to TOOL_HUNK_HEADER_MAX_CHARS (git itself keeps the function
# context short). Enough to navigate a large file, bounded for one with thousands of hunks.
TOOL_MAX_HUNK_HEADERS: Final = 50
TOOL_HUNK_HEADER_MAX_CHARS: Final = 120
# search_diff rejects a pattern before compiling it if it is longer than this, or if its
# counted repeats ({n}, {m,n}) multiply to more than SEARCH_REPEAT_PRODUCT_MAX. The `regex`
# timeout only covers matching; compiling expands counted repeats, so a short pattern like
# (?:x{60000}){60000} could exhaust memory first. Measured in the Phase 5.12 review: at
# these limits the worst pattern compiles in < 0.1 s and ~125 MB.
SEARCH_PATTERN_MAX_CHARS: Final = 500
SEARCH_REPEAT_PRODUCT_MAX: Final = 1_000
# A search_diff call stops after this many seconds of regex matching in total and tells the
# agent to simplify the pattern. The pattern is model-written and runs over untrusted PR text,
# so catastrophic backtracking must not hang the review; the `regex` package enforces it
# inside the matching engine (Python's `re` has no timeout). Plain searches over a large PR
# take milliseconds.
SEARCH_DIFF_TIMEOUT_SECONDS: Final = 2.0
TOOL_PATH_SUGGESTIONS: Final = 3  # closest changed paths offered for an unknown path (difflib)

# Findings the agent is asked for at most (a prompt instruction, not enforced). Enough for a
# large PR's real risks while keeping its final answer, and the synthesizer's input, short.
RISK_AGENT_MAX_FINDINGS: Final = 8

# The hallucination guards (§7) verify evidence only if it has at least this many characters
# after normalising whitespace. Shorter evidence ("=", "return x") is found in almost any
# diff, so it proves nothing; the finding stays unverified and is lowered one level.
MIN_EVIDENCE_CHARS: Final = 12

# ---------------------------------------------------------------------------
# Jev router (§3.3 layer 2, §9 routing failures). Values checked on the real API in
# Phase 6.1. Jev is optional: without JEV_API_KEY the uncertain middle routes complex, and
# PR content is sent to Jev only when the key is set.
# ---------------------------------------------------------------------------

JEV_BASE_URL: Final = "https://api.typesafe.ai"
JEV_ENDPOINT: Final = "/v1/systemone"
# Pinned: `jev-latest` is an alias that moves when TypeSafe ships a release, which would change
# routing with no code change. The response's `model` field confirms what answered (6.1).
JEV_MODEL: Final = "jev-1.14.0"
# Our name for the single yes/no question (a Noul). It's phrased so that "yes" means simple,
# so the answer is P(simple) directly.
JEV_QUESTION: Final = "light_review_enough"

# P(simple) at or above this routes simple, anything below routes complex. Higher than the
# docs' 0.7 starting point on purpose: a needless full review costs cents, a risky PR on the
# light path is a missed risk. The JEV_THRESHOLD environment variable overrides it, read at
# call time, and must lie in [JEV_THRESHOLD_MIN, 1.0]: below 0.5 would route simple when Jev
# leans complex, and 1.0 effectively turns Jev's downgrade off. Not tuned on the 6.1 cases.
JEV_THRESHOLD: Final = 0.85
JEV_THRESHOLD_MIN: Final = 0.5

# httpx applies this to each phase of a request (connect, write, read, pool) and restarts the
# read timer on every chunk received. It is not a deadline for the whole request: a slow
# connect plus a slow write plus a read that trickles bytes can take longer, and there's no
# overall cap (that would need a thread or async; accepted, Phase 6.10 CR4). Measured: a
# whole request takes 0.20–0.71 s (6.1, 6.6).
JEV_TIMEOUT_SECONDS: Final = 3.0
# 2 attempts = 1 immediate retry, only on a timeout, a connection error or a 5xx. A 4xx (429
# included) routes complex at once: the router can't usefully wait out a rate limit, and
# complex is the safe, cheap default.
JEV_MAX_ATTEMPTS: Final = 2

# Payload bounds (§3.3: about 2K tokens; the 6.1 payloads were 1.2–1.4K characters, ~600
# tokens). The title and description are cut with a marker; a title is normally one line, so
# its limit is small. The file list keeps the code files first. Code hunks over
# JEV_DIFF_MAX_CHARS mean Jev isn't asked (router.jev_request says why).
JEV_TITLE_MAX_CHARS: Final = 300
JEV_DESCRIPTION_MAX_CHARS: Final = 1_500
JEV_MAX_FILES_LISTED: Final = 20
JEV_DIFF_MAX_CHARS: Final = 8_000  # ≈ 2K tokens at CHARS_PER_TOKEN
# A model name in a warning (Jev answered with another model) is cut to this length: it's
# response text, so it's bounded like everything else that reaches the trace.
JEV_MODEL_NAME_MAX_CHARS: Final = 60

# List price for jev-1.14.0, $ per million input tokens; output tokens are free (checked
# 2026-11). Used only for the cost estimate in the trace, never for billing.
JEV_PRICE_INPUT_PER_MTOK: Final = 0.042

# ---------------------------------------------------------------------------
# CLI and degradation (§9)
# ---------------------------------------------------------------------------

# A larger PR file exits 2 before it's read. Far above any real PR JSON: a 100-file PR is a
# few MB.
MAX_INPUT_BYTES: Final = 20 * 1024 * 1024
# The error message in a "Degraded: …" trace line: its first line only, cut to this length.
# An API error's message carries the response body, so it's bounded like any other text
# that reaches the trace.
DEGRADED_MESSAGE_MAX_CHARS: Final = 200

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

# A common rough average for English and code. Used only to estimate sizes against the
# budgets, never for billing.
CHARS_PER_TOKEN: Final = 4
PROJECT_CONTEXT_PATH: Final = Path(__file__).parent / "project_context.md"
