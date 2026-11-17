"""Routing for classify_pr (PLAN.md §3.3): layer 1 (hard rules) and layer 3 (fallback).

`decide_route` is the single entry point the classify_pr node calls. The Jev client
(layer 2) comes in Phase 6 and slots in between the rules and the fallback, so the node
doesn't change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from . import config
from .preprocessing import kept_files
from .schemas import FileClassification, PRStats, RouteDecision, ScanHit

# Rule 2 (§3.3): structural flags that force the complex path, as (label, test) pairs.
# Applied to non-filtered files only. Shared with the fallback review in scoring.py.
STRUCTURAL_FLAGS: tuple[tuple[str, Callable[[FileClassification], bool]], ...] = (
    ("core file", lambda c: c.is_core),
    ("sensitive path", lambda c: c.is_sensitive),
    ("new module", lambda c: c.is_new_module),
    ("deleted source file", lambda c: c.status == "deleted" and c.category == "source"),
)


def describe_paths(label: str, paths: Sequence[str]) -> str:
    """'core file `a`' or 'core files `a`, `b` +3 more' (at most ROUTE_REASON_MAX_PATHS)."""
    shown = ", ".join(f"`{p}`" for p in paths[: config.ROUTE_REASON_MAX_PATHS])
    extra = len(paths) - config.ROUTE_REASON_MAX_PATHS
    plural = "s" if len(paths) > 1 else ""
    return f"{label}{plural} {shown}" + (f" +{extra} more" if extra > 0 else "")


def hit_location(hit: ScanHit) -> str:
    return f"`{hit.file}`" if hit.file is not None else "the title/description"


def describe_hit(hit: ScanHit) -> str:
    """Rule name and location only; the matched text is never stored or shown."""
    return f"{hit.kind} scan hit ({hit.pattern}) in {hit_location(hit)}"


def apply_hard_rules(
    classifications: Mapping[str, FileClassification], stats: PRStats
) -> RouteDecision | None:
    """Layer 1 of routing. Returns a rule decision, or None for the uncertain middle.

    The complex rules (1–4) are all evaluated, and the reason lists every one that
    matched, in rule order. Rules 2 and 4 look at non-filtered files only: a filtered
    file (e.g. a formatting-only change to a core file) has nothing to review, so it
    can't force the complex path. A scan hit (rule 1) counts in any file.
    """
    files = list(classifications.values())
    kept = kept_files(classifications)
    reasons: list[str] = []

    # 1. Secret or injection scan hits (capped like the path lists).
    hits = stats.scan_hits
    reasons += [describe_hit(hit) for hit in hits[: config.ROUTE_REASON_MAX_PATHS]]
    if len(hits) > config.ROUTE_REASON_MAX_PATHS:
        reasons.append(f"+{len(hits) - config.ROUTE_REASON_MAX_PATHS} more scan hits")

    # 2. Core, sensitive, new-module or deleted source files.
    for label, test in STRUCTURAL_FLAGS:
        if matches := [c.path for c in kept if test(c)]:
            reasons.append(describe_paths(label, matches))

    # 3. Size.
    if stats.code_files > config.MAX_CODE_FILES:
        reasons.append(f"size: {stats.code_files} code files > {config.MAX_CODE_FILES}")
    if stats.code_lines_changed > config.MAX_CODE_LINES:
        reasons.append(
            f"size: {stats.code_lines_changed} changed code lines > {config.MAX_CODE_LINES}"
        )

    # 4. Files the rules can't judge.
    for label, matches in (
        ("unknown file type", [c.path for c in kept if c.is_unknown_type]),
        ("malformed or missing diff", [c.path for c in kept if c.is_malformed]),
    ):
        if matches:
            reasons.append(describe_paths(label, matches))

    if reasons:
        return RouteDecision(route="complex", source="rule", reason="; ".join(reasons))

    # 5. Only docs and/or mechanical files.
    if all(c.category in ("docs", "mechanical") for c in files):
        docs = sum(c.category == "docs" for c in files)
        return RouteDecision(
            route="simple",
            source="rule",
            reason=f"only docs and/or mechanical files ({docs} docs, {len(files) - docs} mechanical)",
        )

    # 6. The uncertain middle: small code changes the rules can't classify.
    return None


def decide_route(
    classifications: Mapping[str, FileClassification], stats: PRStats
) -> RouteDecision:
    """The full routing decision: the hard rules, else the safe default.

    Without the rules deciding, the PR is in the uncertain middle. Until the Jev client
    exists (Phase 6), Jev is unavailable there, so the route is complex (§3.3, layer 3).
    A fallback is a safe default, not a degraded stage.
    """
    decision = apply_hard_rules(classifications, stats)
    if decision is not None:
        return decision
    return RouteDecision(route="complex", source="fallback", reason="uncertain middle; Jev unavailable")


def describe_route(decision: RouteDecision) -> str:
    """The trace line for a route decision, e.g. 'Route: complex (rule: core file `a.py`).'"""
    return f"Route: {decision.route} ({decision.source}: {decision.reason})."
