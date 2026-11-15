"""Deterministic post-LLM logic: complexity, confidence, draft checks, fallback review.

Design: docs/PLAN.md §8 (`validate`, `finalize`) and §9 (the deterministic fallback review).
Functions are pure: explicit arguments, new objects, no I/O. Output assembly ("path:
concern" strings, `Step N:` numbering, extra focus areas) is the finalize node's job.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from . import config
from .preprocessing import clamp, kept_files, normalize_path
from .router import STRUCTURAL_FLAGS, describe_paths, hit_location
from .schemas import (
    LEVEL_ORDER,
    FileClassification,
    FocusArea,
    Level,
    PRStats,
    ReviewDraft,
    ReviewPlan,
    RiskFindings,
    RouteDecision,
    ScanHit,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round_half_up(value: float, step: float) -> float:
    """Round to the nearest multiple of `step`, halves up (5.25 → 5.5 with step 0.5)."""
    return math.floor(round(value / step, 9) + 0.5) * step


def _forcing_secret_hits(
    scan_hits: list[ScanHit], classifications: Mapping[str, FileClassification]
) -> list[ScanHit]:
    """Secret hits that force risk_level "high": everywhere except test files.

    Test fixtures commonly use fake keys, so a hit in a test file still routes complex
    and is shown to the model, but the model judges its severity (decided in 2.7).
    """
    def in_test_file(hit: ScanHit) -> bool:
        c = classifications.get(hit.file) if hit.file is not None else None
        return c is not None and c.category == "test"

    return [h for h in scan_hits if h.kind == "secret" and not in_test_file(h)]


# ---------------------------------------------------------------------------
# Complexity (§8)
# ---------------------------------------------------------------------------


def complexity_score(classifications: Mapping[str, FileClassification]) -> float:
    """clamp(BASE + size + spread + core + new_modules, MIN, MAX), rounded half-up to 0.5.

    Only non-filtered files count. L = changed lines of source and config files (not
    tests or docs); n = source files. Each term is clamped to [0, its max].
    """
    kept = kept_files(classifications)
    lines = sum(c.lines_added + c.lines_removed for c in kept if c.category in ("source", "config"))
    sources = sum(c.category == "source" for c in kept)

    size = clamp(
        config.COMPLEXITY_SIZE_MAX
        * math.log10(1 + lines)
        / math.log10(1 + config.COMPLEXITY_SIZE_SATURATION_LINES),
        0,
        config.COMPLEXITY_SIZE_MAX,
    )
    spread = clamp(
        config.COMPLEXITY_SPREAD_MAX * (sources - 1) / (config.COMPLEXITY_SPREAD_SATURATION_FILES - 1),
        0,
        config.COMPLEXITY_SPREAD_MAX,
    )
    core = clamp(
        config.COMPLEXITY_CORE_PER_FILE * sum(c.is_core for c in kept), 0, config.COMPLEXITY_CORE_MAX
    )
    new_modules = clamp(
        config.COMPLEXITY_NEW_MODULE_PER_FILE * sum(c.is_new_module for c in kept),
        0,
        config.COMPLEXITY_NEW_MODULE_MAX,
    )
    total = clamp(
        config.COMPLEXITY_BASE + size + spread + core + new_modules,
        config.COMPLEXITY_MIN,
        config.COMPLEXITY_MAX,
    )
    return _round_half_up(total, config.COMPLEXITY_ROUND_STEP)


# ---------------------------------------------------------------------------
# Confidence (§8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceSignals:
    """What the system knows it missed. The defaults mean "nothing wrong"."""

    must_review_coverage: float | None = None  # share of must_review files read; None on the simple path
    truncated: bool = False  # part of a diff wasn't seen (budget, malformed or missing patch)
    disagreement_levels: int | None = None  # |impact_level − max_severity|; None if not comparable
    agent_hit_limit: bool = False
    validation_retried: bool = False
    validation_still_failing: bool = False
    core_files: int = 0  # non-filtered core files changed (their callers are unseen)
    unverified_findings: int = 0
    architecture_degraded: bool = False
    risk_degraded: bool = False


def calibrate_confidence(base: float, signals: ConfidenceSignals) -> tuple[float, list[str]]:
    """Subtract every penalty, then apply every cap with min(), then clamp.

    Returns (confidence rounded to 2 decimals, one trace line per adjustment). A cap is
    traced only when it actually lowers the value.
    """
    trace: list[str] = []
    value = base

    penalties: list[tuple[float, str]] = []
    if signals.disagreement_levels == 1:
        penalties.append((config.CONF_PENALTY_DISAGREE_ONE_LEVEL, "the analysts disagree by one level"))
    if signals.validation_retried:
        penalties.append((config.CONF_PENALTY_VALIDATION_RETRY, "the draft needed a validation retry"))
    if signals.core_files > 0:
        penalties.append((
            min(config.CONF_PENALTY_PER_CORE_FILE * signals.core_files, config.CONF_PENALTY_CORE_FILES_MAX),
            f"{signals.core_files} core file(s) changed; their callers are outside the diff",
        ))
    if signals.unverified_findings > 0:
        penalties.append((
            min(
                config.CONF_PENALTY_PER_UNVERIFIED_FINDING * signals.unverified_findings,
                config.CONF_PENALTY_UNVERIFIED_MAX,
            ),
            f"{signals.unverified_findings} finding(s) couldn't be verified against the diff",
        ))
    for amount, why in penalties:
        value -= amount
        trace.append(f"confidence −{amount:.2f}: {why}")

    caps: list[tuple[float, str]] = []
    if signals.must_review_coverage is not None:
        coverage = clamp(signals.must_review_coverage, 0, 1)
        caps.append((
            config.CONF_CAP_COVERAGE_BASE + config.CONF_CAP_COVERAGE_SLOPE * coverage,
            f"the agent read {coverage:.0%} of the must-review files",
        ))
    if signals.truncated:
        caps.append((config.CONF_CAP_TRUNCATED, "part of the diff wasn't seen"))
    if signals.disagreement_levels is not None and signals.disagreement_levels >= 2:
        caps.append((config.CONF_CAP_DISAGREE_TWO_LEVELS, "the analysts disagree by two levels"))
    if signals.agent_hit_limit:
        caps.append((config.CONF_CAP_AGENT_HIT_LIMIT, "the risk agent hit its budget"))
    if signals.validation_still_failing:
        caps.append((config.CONF_CAP_VALIDATION_STILL_FAILING, "the draft still fails validation"))
    if signals.architecture_degraded:
        caps.append((config.CONF_CAP_ARCHITECTURE_DEGRADED, "the architecture analysis failed"))
    if signals.risk_degraded:
        caps.append((config.CONF_CAP_RISK_DEGRADED, "the risk analysis failed"))
    for cap, why in caps:
        if value > cap:
            value = cap
            trace.append(f"confidence capped at {cap:.2f}: {why}")

    if value > config.CONF_MAX:
        value = config.CONF_MAX
        trace.append(f"confidence capped at {config.CONF_MAX:.2f}: no repo access and no tests run")
    elif value < config.CONF_MIN:
        value = config.CONF_MIN
        trace.append(f"confidence raised to the floor {config.CONF_MIN:.2f}")

    return round(value, 2), trace


# ---------------------------------------------------------------------------
# Draft checks for `validate` (§8)
# ---------------------------------------------------------------------------


def check_draft(
    draft: ReviewDraft,
    *,
    classifications: Mapping[str, FileClassification],
    route: RouteDecision,
    risk: RiskFindings | None,
    scan_hits: list[ScanHit],
) -> list[str]:
    """The three §8 checks. Returns one plain sentence per problem; empty = the draft passes.

    1. Every focus area names a changed file (paths compared after normalising).
    2. The risk level is consistent: on the complex path, not below the highest verified
       risk finding; on both paths, a secret hit outside test files requires "high".
    3. Required fields are non-empty.
    """
    problems: list[str] = []

    changed = {normalize_path(path) for path in classifications}
    for i, area in enumerate(draft.review_focus_areas, start=1):
        if normalize_path(area.file) not in changed:
            problems.append(f"focus area {i} names `{area.file}`, which is not a file in this PR")

    if route.route == "complex" and risk is not None:
        verified: list[Level] = [f.severity for f in risk.findings if f.verified]
        if verified:
            highest = max(verified, key=LEVEL_ORDER.__getitem__)
            if LEVEL_ORDER[draft.risk_level] < LEVEL_ORDER[highest]:
                problems.append(
                    f"risk_level '{draft.risk_level}' is below the highest verified risk finding ('{highest}')"
                )

    forcing = _forcing_secret_hits(scan_hits, classifications)
    if forcing and draft.risk_level != "high":
        where = ", ".join(sorted({hit_location(h) for h in forcing}))
        problems.append(f"a secret scan hit in {where} requires risk_level 'high', not '{draft.risk_level}'")

    for name in ("risk_reasoning", "architectural_impact", "confidence_reasoning"):
        if not getattr(draft, name).strip():
            problems.append(f"{name} is empty")
    for i, area in enumerate(draft.review_focus_areas, start=1):
        if not area.file.strip() or not area.concern.strip():
            problems.append(f"focus area {i} has an empty file or concern")

    return problems


# ---------------------------------------------------------------------------
# Deterministic fallback review (§9)
# ---------------------------------------------------------------------------


def _file_flags(c: FileClassification, hit_kinds: set[str]) -> list[str]:
    flags = [label for label, test in STRUCTURAL_FLAGS if c.filter_reason is None and test(c)]
    if c.is_malformed:
        flags.append("malformed or missing diff")
    flags += [f"{kind} scan hit" for kind in sorted(hit_kinds)]
    if c.filter_reason is not None:
        flags.append(f"mechanical: {c.filter_reason}")
    return flags


def _fallback_focus_areas(
    classifications: Mapping[str, FileClassification], plan: ReviewPlan, hits: list[ScanHit]
) -> list[FocusArea]:
    """must_review first, then the rest of ranked_files, up to MAX_FOCUS_AREAS; the first
    changed paths if every file is mechanical. Each names the file's flags."""
    ordered = list(dict.fromkeys([*plan.must_review, *plan.ranked_files]))
    paths = (ordered or list(classifications))[: config.MAX_FOCUS_AREAS]
    hit_kinds: dict[str, set[str]] = {}
    for h in hits:
        if h.file is not None:
            hit_kinds.setdefault(h.file, set()).add(h.kind)
    focus = []
    for path in paths:
        flags = _file_flags(classifications[path], hit_kinds.get(path, set()))
        detail = f" ({', '.join(flags)})" if flags else ""
        focus.append(FocusArea(file=path, concern=f"not reviewed by the model{detail}; review it manually"))
    return focus


def fallback_review(
    classifications: Mapping[str, FileClassification], plan: ReviewPlan, stats: PRStats
) -> ReviewDraft:
    """A rule-based review for when synthesize fails after its retries (PLAN.md §9).

    Risk: "high" on a secret hit outside test files; "medium" on any non-filtered core,
    sensitive, new-module or deleted source file, an injection hit, or a secret hit in a
    test file; otherwise "low". Focus areas: must_review first, then the rest of
    ranked_files, up to 6; the first 6 changed paths if every file is mechanical.
    """
    kept = kept_files(classifications)
    hits = stats.scan_hits
    forcing = _forcing_secret_hits(hits, classifications)
    test_secrets = [h for h in hits if h.kind == "secret" and h not in forcing]
    injections = [h for h in hits if h.kind == "injection"]

    medium_reasons = [
        describe_paths(label, paths)
        for label, test in STRUCTURAL_FLAGS
        if (paths := [c.path for c in kept if test(c)])
    ]
    medium_reasons += [f"injection scan hit in {hit_location(h)}" for h in injections]
    medium_reasons += [f"secret scan hit in test file {hit_location(h)}" for h in test_secrets]

    level: Level
    if forcing:
        level = "high"
        why = "; ".join(f"secret scan hit ({h.pattern}) in {hit_location(h)}" for h in forcing)
    elif medium_reasons:
        level = "medium"
        why = "; ".join(medium_reasons)
    else:
        level = "low"
        why = "no core, sensitive, new-module or deleted source files and no scan hits"

    return ReviewDraft(
        risk_level=level,
        risk_reasoning=(
            "Deterministic fallback review: the model-written review failed, so this review is "
            f"based on rules only. Risk level '{level}': {why}."
        ),
        architectural_impact=(
            "Not assessed (deterministic fallback). "
            f"{stats.files_total} file(s) changed: {stats.code_files} code file(s), "
            f"{stats.filtered_files} mechanical file(s), {stats.code_lines_changed} changed code line(s)."
        ),
        review_focus_areas=_fallback_focus_areas(classifications, plan, hits),
        confidence=config.FALLBACK_CONFIDENCE,
        confidence_reasoning=(
            f"Fixed at {config.FALLBACK_CONFIDENCE}: rule-based facts only, with no reasoning "
            "about the code's behaviour."
        ),
    )
