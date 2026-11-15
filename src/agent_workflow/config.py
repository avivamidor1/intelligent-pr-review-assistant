"""Deterministic constants: noise filter, categories, priority, routing rules, scoring.

Design: docs/PLAN.md §3.1 (noise filter, categories), §3.2 (priority, review plan),
§3.3 (hard routing rules), §8 (complexity, confidence), §9 (fallback review).

Every number and path pattern used by the deterministic layer is defined here, once.
Regexes that are part of a parsing or scanning algorithm (diff hunks, import lines,
secret and injection rules) live in preprocessing.py, next to the code that uses them.
Models, effort levels, the Jev threshold and environment overrides are added in
Phases 4 and 6, when something uses them.

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

# The diff budget for the architecture analyst's single LLM call, filled in priority order.
# 40K tokens ≈ 160K characters ≈ a few thousand diff lines: enough for full diffs of a
# typical feature PR, while the cost per call stays bounded. Larger PRs are covered by the
# ranked file list and the API-surface extraction (§3.4).
ARCH_DIFF_BUDGET_TOKENS: Final = 40_000

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
# core, sensitive or new), the PR is the "uncertain middle" that Jev decides in Phase 6.
# ---------------------------------------------------------------------------

MAX_CODE_FILES: Final = 3
MAX_CODE_LINES: Final = 50

# A route reason names at most this many files per rule, then "+N more", so a 60-file
# PR still gives a one-line trace entry.
ROUTE_REASON_MAX_PATHS: Final = 5

# Focus areas in a review (draft and final output). Few enough for a reviewer to act on.
MAX_FOCUS_AREAS: Final = 6

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
# Misc
# ---------------------------------------------------------------------------

# A common rough average for English and code. Used only to estimate sizes against the
# budgets, never for billing.
CHARS_PER_TOKEN: Final = 4
PROJECT_CONTEXT_PATH: Final = Path(__file__).parent / "project_context.md"
