"""Deterministic post-LLM logic: complexity, confidence, draft checks, fallback review,
and the output-assembly helpers the finalize node calls.

Design: docs/PLAN.md §3.4 (coverage check, split-PR advice), §8 (`validate`, `finalize`)
and §9 (the deterministic fallback review). Functions are pure: explicit arguments, new
objects, no I/O. The nodes only read the state and call these functions.
"""

from __future__ import annotations

import math
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from . import config
from .preprocessing import clamp, kept_files, normalize_path
from .router import STRUCTURAL_FLAGS, describe_paths, hit_location
from .schemas import (
    LEVEL_ORDER,
    ArchitectureFindings,
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


def _changed_paths(classifications: Mapping[str, FileClassification]) -> set[str]:
    return {normalize_path(path) for path in classifications}


def _highest_verified(route: RouteDecision, risk: RiskFindings | None) -> Level | None:
    """Complex path only: the highest severity among verified risk findings, if any."""
    if route.route != "complex" or risk is None:
        return None
    verified: list[Level] = [f.severity for f in risk.findings if f.verified]
    return max(verified, key=LEVEL_ORDER.__getitem__) if verified else None


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


def _unread_must_review(plan: ReviewPlan, files_inspected: Sequence[str]) -> dict[str, str]:
    """must_review files the risk agent didn't read: normalised path → path as written.

    The single definition of "read", shared by the coverage cap and the focus areas so the
    two can't disagree. In must_review order.
    """
    read = {normalize_path(p) for p in files_inspected}
    unread: dict[str, str] = {}
    for path in plan.must_review:
        if (key := normalize_path(path)) not in read:
            unread.setdefault(key, path)
    return unread


def must_review_coverage(plan: ReviewPlan, files_inspected: Sequence[str]) -> float | None:
    """Share of must_review files the risk agent read (paths normalised).

    None when must_review is empty (e.g. complex only because of a scan hit in the title or
    description): there was nothing the agent had to read, so no coverage cap applies.
    """
    total = len({normalize_path(p) for p in plan.must_review})
    if total == 0:
        return None
    return (total - len(_unread_must_review(plan, files_inspected))) / total


def confidence_signals(
    *,
    route: RouteDecision,
    plan: ReviewPlan,
    classifications: Mapping[str, FileClassification],
    architecture: ArchitectureFindings | None,
    risk: RiskFindings | None,
    files_inspected: Sequence[str],
    risk_budget_exhausted: bool,
    revision_count: int,
    validation_errors: Sequence[str],
    degraded: Sequence[str],
) -> ConfidenceSignals:
    """Map the review's state to the §8 confidence signals.

    `degraded` holds node names. Until the Phase 5 guards run, every risk finding counts as
    unverified (`verified` defaults to False): the conservative default. `truncated` covers
    malformed or missing diffs; Phase 4 adds files cut by the architecture diff budget.
    """
    kept = kept_files(classifications)
    disagreement: int | None = None
    if architecture is not None and risk is not None and risk.max_severity is not None:
        disagreement = abs(LEVEL_ORDER[architecture.impact_level] - LEVEL_ORDER[risk.max_severity])
    return ConfidenceSignals(
        must_review_coverage=(
            must_review_coverage(plan, files_inspected) if route.route == "complex" else None
        ),
        truncated=any(c.is_malformed for c in kept),
        disagreement_levels=disagreement,
        agent_hit_limit=risk_budget_exhausted,
        validation_retried=revision_count > 0,
        validation_still_failing=bool(validation_errors),
        core_files=sum(c.is_core for c in kept),
        unverified_findings=sum(not f.verified for f in risk.findings) if risk is not None else 0,
        architecture_degraded="architecture_analyst" in degraded,
        risk_degraded="risk_agent" in degraded,
    )


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

    changed = _changed_paths(classifications)
    for i, area in enumerate(draft.review_focus_areas, start=1):
        if normalize_path(area.file) not in changed:
            problems.append(f"focus area {i} names `{area.file}`, which is not a file in this PR")

    highest = _highest_verified(route, risk)
    if highest is not None and LEVEL_ORDER[draft.risk_level] < LEVEL_ORDER[highest]:
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


def repair_draft(
    draft: ReviewDraft,
    *,
    classifications: Mapping[str, FileClassification],
    route: RouteDecision,
    plan: ReviewPlan,
    risk: RiskFindings | None,
    scan_hits: list[ScanHit],
) -> tuple[ReviewDraft, list[str]]:
    """Fix what check_draft flags and rules can fix, for a draft still failing after its
    retry (§8). Returns (the repaired draft, one trace line per repair).

    - The risk level is raised to the minimum the checks require: the highest verified risk
      finding (complex path), and "high" for a secret hit outside test files (the 2.7
      exception: a secret hit in a test file alone doesn't force "high"). The reasoning
      then starts with a "[Repaired: …]" note, so it doesn't argue for the old level.
    - Focus areas naming a file not in the PR are dropped. If none is left, the rule-based
      focus areas (as in the fallback review) take their place, so the draft keeps 1–6.

    Empty fields aren't repaired. A no-op on a draft that passes check_draft, since it
    applies the same rules. The confidence cap (0.5) comes from validation_still_failing.
    """
    trace: list[str] = []
    level = draft.risk_level
    raised_for: list[str] = []
    floors: list[tuple[Level, str]] = []
    if (highest := _highest_verified(route, risk)) is not None:
        floors.append((highest, "the highest verified risk finding"))
    if _forcing_secret_hits(scan_hits, classifications):
        floors.append(("high", "a secret scan hit outside test files"))
    for floor, why in floors:
        if LEVEL_ORDER[level] < LEVEL_ORDER[floor]:
            trace.append(f"Repair: risk_level raised from '{level}' to '{floor}' ({why}).")
            level = floor
            raised_for.append(why)
    reasoning = draft.risk_reasoning
    if raised_for:
        reasoning = (
            f"[Repaired: risk level raised from '{draft.risk_level}' to '{level}': "
            f"{'; '.join(raised_for)}.] {reasoning}"
        )

    changed = _changed_paths(classifications)
    areas: list[FocusArea] = []
    dropped: list[str] = []
    for area in draft.review_focus_areas:
        if normalize_path(area.file) in changed:
            areas.append(area)
        else:
            dropped.append(f"`{area.file}`")
    if dropped:
        trace.append(f"Repair: dropped focus area(s) naming files not in this PR: {', '.join(dropped)}.")
    if not areas:
        areas = _fallback_focus_areas(classifications, plan, scan_hits)
        trace.append("Repair: no valid focus area left; used the rule-based focus areas.")

    repaired = draft.model_copy(
        update={"risk_level": level, "risk_reasoning": reasoning, "review_focus_areas": areas}
    )
    return repaired, trace


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


# ---------------------------------------------------------------------------
# Output assembly for `finalize` (§3.4, §8)
# ---------------------------------------------------------------------------

NOT_INSPECTED = "not inspected by the risk agent"  # the per-file suffix and the combined item


def split_pr_recommendation(classifications: Mapping[str, FileClassification]) -> str | None:
    """PR-wide advice to split a PR that is both large AND spread out (§3.4, decided in 3.1).

    Counts non-filtered source files and their modules (distinct parent folders). Both
    counts must exceed their limits. Advice only: it never changes the route or the risk.
    """
    sources = [normalize_path(c.path) for c in kept_files(classifications) if c.category == "source"]
    source_count = len(sources)
    module_count = len({posixpath.dirname(path) for path in sources})
    if source_count > config.SPLIT_PR_MAX_SOURCE_FILES and module_count > config.SPLIT_PR_MAX_MODULES:
        return (
            f"PR-wide: {source_count} source files across {module_count} modules; "
            "consider splitting it into smaller PRs"
        )
    return None


def final_focus_areas(
    areas: Sequence[FocusArea],
    *,
    route: RouteDecision,
    plan: ReviewPlan,
    classifications: Mapping[str, FileClassification],
    files_inspected: Sequence[str],
) -> tuple[list[str], list[str]]:
    """The output's focus areas as "path: concern" strings, plus trace lines.

    Order: the split-PR advice (if any), then the draft's areas, written with the PR's own
    path (duplicates removed: same file and concern), then one combined "not inspected" item. Coverage check
    (complex path only): an unread must_review file the draft names gets a suffix; the
    unread files it doesn't name are listed together in that one PR-wide item. The PR-wide
    items have reserved slots, so when the list is over MAX_FOCUS_AREAS, the last draft
    areas are cut, never an unread file. The combined item names up to 5 files, then
    "+N more"; the trace names every unread file and counts any cut areas.
    """
    unread = _unread_must_review(plan, files_inspected) if route.route == "complex" else {}

    # (normalised file, "path: concern") per area. The path is the PR's, not the model's
    # spelling (`./src/a.py` → `src/a.py` as the PR writes it).
    pr_paths = {normalize_path(path): path for path in classifications}
    entries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for area in areas:
        key = normalize_path(area.file)
        if (key, area.concern) in seen:
            continue
        seen.add((key, area.concern))
        suffix = f" (verify: {NOT_INSPECTED})" if key in unread else ""
        entries.append((key, f"{pr_paths.get(key, area.file)}: {area.concern}{suffix}"))

    def not_named_in(areas_kept: list[tuple[str, str]]) -> list[str]:
        """Unread files no kept draft area names: they go in the combined item."""
        named = {key for key, _ in areas_kept}
        return [path for key, path in unread.items() if key not in named]

    split = split_pr_recommendation(classifications)
    head = [split] if split is not None else []
    room = config.MAX_FOCUS_AREAS - len(head)
    kept = entries[:room]
    # Reserve a slot for the combined item, then recheck: a draft area cut to make room
    # may have been the only one naming an unread file, which then joins the combined item.
    if not_named_in(kept):
        kept = entries[: room - 1]
    tail: list[str] = []
    if not_named := not_named_in(kept):
        tail.append(
            f"PR-wide: verify: {describe_paths('must-review file', not_named)} {NOT_INSPECTED}"
        )

    trace: list[str] = []
    if unread:
        names = ", ".join(f"`{path}`" for path in unread.values())
        trace.append(f"Not inspected by the risk agent: {names}.")
    if (cut := len(entries) - len(kept)) > 0:
        trace.append(f"{cut} draft focus area(s) cut by the limit of {config.MAX_FOCUS_AREAS}.")
    return [*head, *(text for _, text in kept), *tail], trace


def number_trace(lines: Sequence[str]) -> list[str]:
    """["Step 1: …", "Step 2: …", …] for the output's reasoning_trace (§8)."""
    return [f"Step {i}: {line}" for i, line in enumerate(lines, start=1)]
