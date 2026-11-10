"""Pydantic models: PR input, internal working data, and the final review output.

Design: docs/PLAN.md §1 (output fields), §6 (state), §8 (draft and output), §9 (input handling).

Input models (`ChangedFile`, `PRInput`) hold only data received from the PR. Everything the
system derives (status mapping, recomputed line counts, categories, flags, priority) lives in
`FileClassification`, which preprocessing builds and which is never validated from the input.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, StringConstraints, field_validator
from pydantic.json_schema import SkipJsonSchema

Level = Literal["low", "medium", "high"]
FileStatus = Literal["added", "modified", "deleted", "renamed"]
FileCategory = Literal["mechanical", "test", "docs", "config", "source"]

_LEVEL_ORDER: dict[Level, int] = {"low": 0, "medium": 1, "high": 2}

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# ---------------------------------------------------------------------------
# Input (raw PR data only)
# ---------------------------------------------------------------------------


class ChangedFile(BaseModel):
    # Unknown keys, including any classification-like keys ("category", "priority", ...),
    # are dropped: classification is never accepted from the input.
    model_config = ConfigDict(extra="ignore")

    path: NonEmptyStr
    status: NonEmptyStr  # raw value; mapped to FileStatus by preprocessing (FileClassification.status)
    lines_added: NonNegativeInt  # as reported; preprocessing recomputes from the diff
    lines_removed: NonNegativeInt
    diff: str


class PRInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pr_title: str
    pr_description: str = ""
    author: str = ""
    changed_files: list[ChangedFile] = Field(min_length=1)
    # Present in the assignment format; optional because preprocessing recomputes from the diffs.
    base_commit: str | None = None
    head_commit: str | None = None
    files_changed: NonNegativeInt | None = None
    total_additions: NonNegativeInt | None = None
    total_deletions: NonNegativeInt | None = None

    @field_validator("pr_description", "author", mode="before")
    @classmethod
    def _none_to_empty(cls, value: Any) -> Any:
        return "" if value is None else value


# ---------------------------------------------------------------------------
# Internal working data (system-generated)
# ---------------------------------------------------------------------------


class FileClassification(BaseModel):
    """Everything parse_pr derives about one changed file (PLAN.md §3.1–3.2, §9)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    status: FileStatus  # mapped from the raw status
    status_was_unknown: bool = False  # raw status wasn't recognised and was mapped to "modified"
    lines_added: NonNegativeInt  # recomputed from the diff
    lines_removed: NonNegativeInt
    category: FileCategory
    filter_reason: str | None = None  # set only for mechanical files, e.g. "lockfile", "formatting only"
    is_core: bool = False
    is_sensitive: bool = False
    is_new_module: bool = False
    is_unknown_type: bool = False  # categorised as source by default (§3.1)
    is_malformed: bool = False  # non-empty diff with no @@ hunks
    priority: float


class ScanHit(BaseModel):
    kind: Literal["secret", "injection"]
    file: str | None  # None = PR title or description
    pattern: str  # name of the rule that matched; never the matched secret itself


class PRStats(BaseModel):
    """PR-level aggregates. Per-file facts live in FileClassification."""

    files_total: int
    code_files: int  # source + config + test
    source_files: int
    filtered_files: int  # mechanical files, never sent to analysts
    lines_added: int
    lines_removed: int
    code_lines_changed: int
    estimated_tokens: int
    scan_hits: list[ScanHit] = Field(default_factory=list)


class ReviewPlan(BaseModel):
    ranked_files: list[str]  # every non-filtered file, highest priority first
    must_review: list[str]
    architecture_diff_budget_tokens: int
    max_tool_calls: int


class RouteDecision(BaseModel):
    route: Literal["simple", "complex"]
    source: Literal["rule", "jev", "fallback"]
    probability: float | None = Field(default=None, ge=0, le=1)  # P(simple), set only when Jev decided
    reason: str


class Finding(BaseModel):
    file: str
    description: str
    evidence: str  # must appear in the diff; checked by the hallucination guards
    severity: Level
    # System-set by the hallucination guards (PLAN.md §7); hidden from the schema the model fills.
    verified: SkipJsonSchema[bool] = True


class ArchitectureFindings(BaseModel):
    impact_level: Level
    summary: str
    findings: list[Finding] = Field(default_factory=list)


class RiskFindings(BaseModel):
    findings: list[Finding] = Field(default_factory=list)

    @property
    def max_severity(self) -> Level | None:
        """Highest finding severity, or None if there are no findings.

        A property rather than a field so it stays out of the schema the model fills.
        """
        if not self.findings:
            return None
        return max((f.severity for f in self.findings), key=_LEVEL_ORDER.__getitem__)


class FocusArea(BaseModel):
    file: str
    concern: str


class ReviewDraft(BaseModel):
    risk_level: Level
    risk_reasoning: str
    architectural_impact: str
    review_focus_areas: list[FocusArea] = Field(min_length=1, max_length=6)
    confidence: float = Field(ge=0, le=1)
    confidence_reasoning: str


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: Level
    risk_reasoning: str
    architectural_impact: str
    review_focus_areas: list[str] = Field(max_length=6)  # "path: concern"
    complexity_score: float = Field(ge=1, le=10)
    confidence: float = Field(ge=0, le=1)
    reasoning_trace: list[str]
