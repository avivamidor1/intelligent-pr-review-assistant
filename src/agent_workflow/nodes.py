"""Graph nodes (docs/PLAN.md §2).

Each node reads the state and returns a partial update. The decisions themselves live in
pure functions (preprocessing.py, router.py, scoring.py), so the nodes stay thin. The LLM
nodes reach the model only through `llm.structured_call` and build their prompts with
prompts.py; a test can patch `llm.structured_call` to run them without an API key.

Trace lines are plain sentences; `finalize` numbers them. `degraded` holds node names.
"""

from __future__ import annotations

from typing import Any

from . import config, llm, prompts
from .preprocessing import api_surface, load_core_patterns, preprocess, select_diffs
from .router import decide_route, describe_paths, describe_route
from .schemas import ArchitectureFindings, PRInput, ReviewDraft, ReviewOutput
from .scoring import (
    calibrate_confidence,
    check_draft,
    complexity_score,
    confidence_signals,
    final_focus_areas,
    number_trace,
    repair_draft,
)
from .state import ReviewState


def _diff_budget(state: ReviewState) -> int:
    """Tokens of raw diff the route's diff-reading LLM call gets: the architecture analyst's
    on the complex path, the synthesizer's on the simple path. One definition, shared by
    those nodes and by finalize's truncation check (D2), so they can't disagree."""
    if state["route"].route == "complex":
        return state["plan"].architecture_diff_budget_tokens
    return config.SYNTH_SIMPLE_DIFF_BUDGET_TOKENS


def _require_draft(state: ReviewState, node: str) -> ReviewDraft:
    """synthesize always runs before validate and finalize; a missing draft is a wiring bug."""
    draft = state["draft"]
    if draft is None:
        raise RuntimeError(f"{node} reached without a draft")
    return draft


# ---------------------------------------------------------------------------
# Deterministic head
# ---------------------------------------------------------------------------


def parse_pr(state: ReviewState) -> dict[str, Any]:
    """Validate the raw PR and run every deterministic preprocessing step (§3.1–3.2, §9).

    A ValidationError propagates: the CLI validates the input first and exits 2 (Phase 7).
    A missing project_context.md raises too, on purpose: silently losing the core patterns
    would mean fewer complex routes.
    """
    pr = PRInput.model_validate(state["raw_pr"])
    core_patterns = load_core_patterns(config.PROJECT_CONTEXT_PATH.read_text(encoding="utf-8"))
    result = preprocess(pr, core_patterns)
    stats, plan = result.stats, result.plan

    must_review = (
        f"must-review {describe_paths('file', plan.must_review)}" if plan.must_review else "must-review: none"
    )
    trace = [
        *result.notes,
        f"Parsed {stats.files_total} file(s): {stats.code_files} code ({stats.source_files} source), "
        f"{stats.filtered_files} mechanical skipped; {stats.code_lines_changed} changed code lines; "
        f"~{stats.estimated_tokens} tokens.",
        f"Review plan: {must_review}; tool budget {plan.max_tool_calls} calls; "
        f"architecture diff budget {plan.architecture_diff_budget_tokens} tokens.",
    ]
    return {
        "pr": result.pr,
        "classifications": result.classifications,
        "stats": stats,
        "plan": plan,
        # Every field a later node may read before it's written starts set. (`route` is set
        # by classify_pr before any read; `degraded` starts as [] from its reducer.)
        "architecture": None,
        "risk": None,
        "files_inspected": [],
        "risk_budget_exhausted": False,
        "draft": None,
        "validation_errors": [],
        "revision_count": 0,
        "reasoning_trace": trace,
    }


def classify_pr(state: ReviewState) -> dict[str, Any]:
    """Choose the simple or complex path (§3.3). The conditional edge after it reads `route`."""
    route = decide_route(state["classifications"], state["stats"])
    return {"route": route, "reasoning_trace": [describe_route(route)]}


# ---------------------------------------------------------------------------
# LLM nodes. An LLM failure raises until Phase 7 adds degradation; risk_agent is a stub
# until Phase 5.
# ---------------------------------------------------------------------------


def architecture_analyst(state: ReviewState) -> dict[str, Any]:
    """Architectural impact in one LLM call (§2, §3.4): the ranked files, the API surface,
    and the full diffs that fit the architecture budget."""
    pr, plan = state["pr"], state["plan"]
    included, cut = select_diffs(pr, plan, _diff_budget(state))
    findings, usage = llm.structured_call(
        ArchitectureFindings,
        effort=config.EFFORT_ARCHITECTURE,
        system=prompts.architecture_system(),
        user=prompts.architecture_user(
            pr, state["classifications"], plan, state["stats"],
            surface=api_surface(pr, plan), included=included, cut=cut,
        ),
    )
    trace = [f"Architecture analyst: impact {findings.impact_level}, {len(findings.findings)} finding(s)."]
    if cut:
        trace.append(f"Architecture analyst: {describe_paths('file', cut)} over the diff budget, not shown.")
    trace.append(usage.trace_line("architecture_analyst"))
    return {"architecture": findings, "reasoning_trace": trace}


def risk_agent(state: ReviewState) -> dict[str, Any]:
    """The ReAct risk agent (§7). STUB: replaced in Phase 5.

    It doesn't run, so it reports no result (`risk=None`): the synthesizer is told the risk
    review is "not available", never that it completed and found nothing. Nothing is read,
    so files_inspected stays empty and the coverage cap applies, honestly.
    """
    # STUB: replaced in Phase 5.
    return {
        "risk": None,
        "files_inspected": [],
        "risk_budget_exhausted": False,
        "reasoning_trace": ["Risk agent: stub, not run (Phase 5); no risk review."],
    }


def synthesize(state: ReviewState) -> dict[str, Any]:
    """Write the review draft in one LLM call (§8): the small diff itself on the simple
    path, the analysts' findings on the complex path. A run with validation errors in the
    state is a revision: the previous draft and the errors go into the prompt."""
    pr, classifications, plan, stats = state["pr"], state["classifications"], state["plan"], state["stats"]
    route = state["route"].route
    errors = state["validation_errors"]
    previous = state["draft"] if errors else None

    trace: list[str] = []
    revision_count = state["revision_count"]
    if errors:  # the validator sent the draft back
        revision_count += 1
        trace.append(f"Synthesize: revision {revision_count} with the validator's feedback.")

    effort: llm.Effort
    if route == "simple":
        included, cut = select_diffs(pr, plan, _diff_budget(state))
        if cut and not errors:  # traced once, not again on a revision
            trace.append(f"Synthesize: {describe_paths('file', cut)} over the diff budget, not shown.")
        effort = config.EFFORT_SYNTH_SIMPLE
        user = prompts.synthesize_simple_user(
            pr, classifications, plan, stats,
            included=included, cut=cut, validation_errors=errors, previous=previous,
        )
    else:
        effort = config.EFFORT_SYNTH_COMPLEX
        user = prompts.synthesize_complex_user(
            pr, classifications, plan, stats,
            architecture=state["architecture"], risk=state["risk"],
            validation_errors=errors, previous=previous,
        )
    draft, usage = llm.structured_call(ReviewDraft, effort=effort, system=prompts.SYNTHESIZE_SYSTEM, user=user)
    trace += [
        f"Synthesize: draft risk {draft.risk_level} ({route} path, effort {effort}).",
        usage.trace_line("synthesize"),
    ]
    return {"draft": draft, "revision_count": revision_count, "reasoning_trace": trace}


# ---------------------------------------------------------------------------
# Deterministic tail
# ---------------------------------------------------------------------------


def validate(state: ReviewState) -> dict[str, Any]:
    """The §8 draft checks. The edge after it sends a failing draft back to synthesize once."""
    problems = check_draft(
        _require_draft(state, "validate"),
        classifications=state["classifications"],
        route=state["route"],
        risk=state["risk"],
        scan_hits=state["stats"].scan_hits,
    )
    line = f"Validation failed: {'; '.join(problems)}." if problems else "Validation passed."
    return {"validation_errors": problems, "reasoning_trace": [line]}


def finalize(state: ReviewState) -> dict[str, Any]:
    """Score, calibrate, repair a still-failing draft, and assemble the output (§8). Phase 7
    replaces the missing-draft error with the deterministic fallback review."""
    draft = _require_draft(state, "finalize")
    classifications, route, plan = state["classifications"], state["route"], state["plan"]
    revisions, errors = state["revision_count"], state["validation_errors"]
    files_inspected, scan_hits = state["files_inspected"], state["stats"].scan_hits

    score = complexity_score(classifications)
    # Recomputed, not stored (D2): select_diffs is pure, so this is the node's own selection.
    _, diffs_cut = select_diffs(state["pr"], plan, _diff_budget(state))
    signals = confidence_signals(
        route=route,
        plan=plan,
        classifications=classifications,
        architecture=state["architecture"],
        risk=state["risk"],
        files_inspected=files_inspected,
        risk_budget_exhausted=state["risk_budget_exhausted"],
        revision_count=revisions,
        validation_errors=errors,
        degraded=state["degraded"],
        scan_hits=scan_hits,
        diffs_cut=diffs_cut,
    )
    confidence, confidence_lines = calibrate_confidence(draft.confidence, signals)
    # A no-op unless the draft still fails validation after its retry (§8).
    repaired, repair_lines = repair_draft(
        draft,
        classifications=classifications,
        route=route,
        plan=plan,
        risk=state["risk"],
        scan_hits=scan_hits,
    )
    focus, focus_lines = final_focus_areas(
        repaired.review_focus_areas,
        route=route,
        plan=plan,
        classifications=classifications,
        files_inspected=files_inspected,
        scan_hits=scan_hits,
    )

    # number_trace collapses each line, so model text can't break the `Step N:` format.
    lines = [
        f"Model confidence {draft.confidence:.2f}: {draft.confidence_reasoning}",
        *confidence_lines,
        f"Confidence: {confidence:.2f}.",
        f"Complexity score: {score}.",
        *focus_lines,
    ]
    if errors:
        retries = f"{revisions} {'retry' if revisions == 1 else 'retries'}"
        lines.append(f"Validation still failing after {retries}: {'; '.join(errors)}.")
    lines += repair_lines

    output = ReviewOutput(
        risk_level=repaired.risk_level,
        risk_reasoning=repaired.risk_reasoning,
        architectural_impact=repaired.architectural_impact,
        review_focus_areas=focus,
        complexity_score=score,
        confidence=confidence,
        reasoning_trace=number_trace([*state["reasoning_trace"], *lines]),
    )
    return {"output": output}
