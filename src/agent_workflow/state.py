"""Main graph state (docs/PLAN.md §6).

The caller sees only GraphInput → GraphOutput (the review, and which stages degraded);
everything else is internal working memory. The risk agent's own state (RiskAgentState, a create_agent state schema) lives in risk_agent.py.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from agent_workflow.schemas import (
    ArchitectureFindings,
    FileClassification,
    PRInput,
    PRStats,
    ReviewDraft,
    ReviewOutput,
    ReviewPlan,
    RiskFindings,
    RouteDecision,
)


class ReviewState(TypedDict):
    raw_pr: dict[str, Any]  # input
    pr: PRInput  # validated input data only (duplicate paths merged); no system-generated fields
    classifications: dict[str, FileClassification]  # path → derived facts (set by parse_pr)
    stats: PRStats  # counts (incl. filtered), token estimate, scan hits
    plan: ReviewPlan  # ranked_files, must_review, budgets (set by parse_pr)
    route: RouteDecision  # set by classify_pr
    architecture: ArchitectureFindings | None
    risk: RiskFindings | None
    files_inspected: list[str]  # what the agent actually read
    risk_budget_exhausted: bool  # risk agent ran out of its tool/model-call budget (§7); caps confidence (§8)
    draft: ReviewDraft | None
    validation_errors: list[str]
    revision_count: int
    degraded: Annotated[list[str], operator.add]  # stages that failed or fell back
    reasoning_trace: Annotated[list[str], operator.add]
    output: ReviewOutput | None
    fallback: bool  # finalize wrote the deterministic fallback review (§9)


class GraphInput(TypedDict):
    raw_pr: dict[str, Any]


class GraphOutput(TypedDict):
    output: ReviewOutput | None
    degraded: list[str]  # the stages that failed; the CLI exits 3 when it's non-empty (§9)
    fallback: bool  # the review is the rule-based fallback; the CLI says so on stderr (§9)
