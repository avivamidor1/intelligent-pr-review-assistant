"""The ReAct risk agent (docs/PLAN.md §7): `create_agent` with explicit middleware.

    START → model ─┬─ tool calls ──► tools ──► model   (bounded by the limit middleware)
                   └─ final answer ─► END     (structured_response: RiskFindings)

Every behaviour the loop has is chosen here and covered by a test:
- the tool budget: over-budget calls get an error ToolMessage and the model goes on,
- the model-call backstop: ends the run if the model keeps asking for blocked tools,
- tool errors become error ToolMessages the agent can act on (tools.on_tool_error),
- a refusal is an error, never a final answer,
- the final answer is native JSON-schema structured output (ProviderStrategy, never
  ToolStrategy, which would force tool_choice: a 400 with thinking on, §5),
- a recursion limit computed from the budgets (create_agent's own default is 9,999).

`run_risk_agent` is the wrapper the graph node calls: it builds the agent for this PR's
budgets, runs it, and makes the recovery call when the run ends without a parsed answer.
The agent's message history stays here; only the findings and run facts go back.

Print the agent's graph (no API key needed):

    uv run python -m agent_workflow.risk_agent
"""

from __future__ import annotations

import operator
import re
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Literal

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelCallLimitMiddleware,
    ModelRefusalMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
)
from langchain.agents.structured_output import ProviderStrategy, StructuredOutputValidationError
from langchain_core.exceptions import ModelRefusalError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph

from . import config, llm, prompts
from .preprocessing import changed_files_by_path, diff_body_lines, normalize_path
from .router import describe_paths
from .schemas import Level, PRInput, ReviewPlan, RiskFindings
from .state import ReviewState
from .tools import RISK_TOOLS, RiskAgentContext, on_tool_error

# The content ToolCallLimitMiddleware gives a blocked call (checked in Phase 5.1).
_BLOCKED = "Tool call limit exceeded."

RecoveryReason = Literal["model_call_limit", "unparsed_answer"]


class RiskAgentState(AgentState):
    """create_agent's state plus the diff pages the agent read (§6): (path, page, pages).
    read_file_diff records each read with Command(update=...); the reducer is safe for
    parallel tool calls. A file counts as inspected only once every page was read."""

    pages_read: Annotated[list[tuple[str, int, int]], operator.add]


@dataclass(frozen=True)
class RiskRun:
    """What one agent run produced. The message history isn't kept."""

    findings: RiskFindings
    files_inspected: list[str]  # every page read, in the order of first reads
    files_partly_read: list[str]  # some pages read: not inspected (coverage, §3.4)
    budget_exhausted: bool  # wanted more tool calls than its budget, or hit the model-call limit (§7)
    recovery_reason: RecoveryReason | None  # why the recovery call was made, if it was
    tool_calls: dict[str, int]  # requested calls by tool name (including blocked ones)
    blocked_calls: int  # over the tool budget
    tool_errors: int  # error ToolMessages other than blocked calls
    usage: list[llm.Usage]  # one per agent model call
    recovery_usage: llm.Usage | None
    max_tool_calls: int
    max_model_calls: int


def budgets(plan: ReviewPlan) -> tuple[int, int, int]:
    """(max_tool_calls, max_model_calls, recursion_limit) for this PR (§7)."""
    max_tool_calls = plan.max_tool_calls
    max_model_calls = max_tool_calls + config.MODEL_CALL_SLACK
    recursion_limit = config.RISK_AGENT_STEPS_PER_TURN * (max_model_calls + 1)
    return max_tool_calls, max_model_calls, recursion_limit


def build_risk_agent(
    model: BaseChatModel,
    *,
    max_tool_calls: int,
    max_model_calls: int,
    provider_middleware: list[AgentMiddleware] | None = None,
) -> CompiledStateGraph:
    """The agent for one run (its budgets depend on the PR).

    Middleware, in this order:
    1. the tool budget: blocked calls get an error ToolMessage, the run continues,
    2. the model-call backstop: ends the run before a call over the limit,
    3. tool errors → error ToolMessages (our handler; argument errors are ToolNode's),
    4. refusals → ModelRefusalError (the wrapper raises LLMRefusal),
    5. the provider's middleware (prompt caching, from llm.agent_middleware).
    The two limits are graph hooks (before/after the model node); the other three wrap the
    model or tool calls, and caching goes last (innermost) so it marks the request exactly
    as sent.
    """
    middleware: list[AgentMiddleware] = [
        ToolCallLimitMiddleware(run_limit=max_tool_calls, exit_behavior="continue"),
        ModelCallLimitMiddleware(run_limit=max_model_calls, exit_behavior="end"),
        ToolErrorMiddleware(on_tool_error),
        ModelRefusalMiddleware(),
        *(provider_middleware or []),
    ]
    return create_agent(
        model,
        RISK_TOOLS,
        system_prompt=prompts.RISK_AGENT_SYSTEM,
        middleware=middleware,
        response_format=ProviderStrategy(RiskFindings),
        state_schema=RiskAgentState,
        context_schema=RiskAgentContext,
        name="risk_agent",
    )


def _read_coverage(pages_read: list[tuple[str, int, int]]) -> tuple[list[str], list[str]]:
    """(fully read, partly read) paths, in the order of first reads. A file is fully read
    once every one of its pages was read, in any order; reading page 1 of 10 isn't."""
    seen: dict[str, set[int]] = {}
    total: dict[str, int] = {}
    for path, page, pages in pages_read:
        seen.setdefault(path, set()).add(page)
        total[path] = pages
    full = [path for path, read in seen.items() if len(read) == total[path]]
    partly = [path for path, read in seen.items() if len(read) < total[path]]
    return full, partly


def run_risk_agent(state: ReviewState) -> RiskRun:
    """Build and run the agent for this PR, then make the recovery call if needed (§7).

    The agent is streamed, keeping the last state: a final answer that doesn't parse raises
    out of the run, and the recovery call needs the history (Phase 5.1). Raises
    llm.LLMRefusal on a refusal; GraphRecursionError and other errors propagate (the node
    fails; Phase 7 degrades it). The recovery call raises LLMSchemaError if it can't parse
    either. Callbacks (Langfuse) come from the graph's config, so the agent nests under
    its node.
    """
    max_tool_calls, max_model_calls, recursion_limit = budgets(state["plan"])
    agent = build_risk_agent(
        llm.agent_model(config.EFFORT_RISK_AGENT),
        max_tool_calls=max_tool_calls,
        max_model_calls=max_model_calls,
        provider_middleware=llm.agent_middleware(),
    )
    context = RiskAgentContext(
        pr=state["pr"],
        classifications=state["classifications"],
        project_context=config.PROJECT_CONTEXT_PATH.read_text(encoding="utf-8"),
    )
    first = HumanMessage(
        prompts.risk_agent_user(
            state["pr"], state["classifications"], state["plan"], state["stats"], max_tool_calls=max_tool_calls
        )
    )

    last: dict = {"messages": [first], "pages_read": []}
    bad_answer: AIMessage | None = None  # set when the final answer didn't parse
    try:
        for snapshot in agent.stream(
            {"messages": [first]},
            config={"recursion_limit": recursion_limit},
            context=context,
            stream_mode="values",
        ):
            last = snapshot
    except StructuredOutputValidationError as err:
        # The history up to the bad answer is in `last`. The answer itself isn't in the
        # state, but it was a real (billed) model call.
        bad_answer = err.ai_message
    except ModelRefusalError as err:
        raise llm.LLMRefusal(f"the risk agent's model declined: {err}") from err

    messages: list[BaseMessage] = last["messages"]
    # After a bad answer, `last` predates it, so it has no structured_response either.
    answer: RiskFindings | None = last.get("structured_response")
    recovery_reason: RecoveryReason | None = None
    recovery_usage: llm.Usage | None = None
    if answer is None:
        # Not parsed, or the model-call limit ended the run (structured_response is None).
        recovery_reason = "unparsed_answer" if bad_answer is not None else "model_call_limit"
        answer, recovery_usage = llm.structured_call_messages(
            RiskFindings,
            effort=config.EFFORT_RISK_AGENT,
            system=prompts.RISK_AGENT_SYSTEM,
            messages=[*messages, HumanMessage(prompts.RISK_RECOVERY_INSTRUCTION)],
        )

    ai = [m for m in messages if isinstance(m, AIMessage)]
    requested = Counter(call["name"] for m in ai for call in m.tool_calls)
    tool_results = [m for m in messages if isinstance(m, ToolMessage)]
    blocked = sum(str(m.content).startswith(_BLOCKED) for m in tool_results)
    errors = sum(m.status == "error" for m in tool_results) - blocked
    # Only real model calls carry usage; the limit middleware's closing message doesn't.
    model_replies = ai + ([bad_answer] if bad_answer is not None else [])
    fully_read, partly_read = _read_coverage(last.get("pages_read") or [])
    return RiskRun(
        findings=answer,
        files_inspected=fully_read,
        files_partly_read=partly_read,
        budget_exhausted=sum(requested.values()) > max_tool_calls or recovery_reason == "model_call_limit",
        recovery_reason=recovery_reason,
        tool_calls=dict(requested),
        blocked_calls=blocked,
        tool_errors=errors,
        usage=[llm.usage_of(m) for m in model_replies if m.response_metadata.get("usage")],
        recovery_usage=recovery_usage,
        max_tool_calls=max_tool_calls,
        max_model_calls=max_model_calls,
    )


# ---------------------------------------------------------------------------
# Hallucination guards (§7, D5): deterministic, after the run
# ---------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")
_DEMOTED: dict[Level, Level] = {"high": "medium", "medium": "low", "low": "low"}


def _normalise(lines: list[str]) -> str:
    """The lines joined, every whitespace run collapsed to one space."""
    return _WHITESPACE.sub(" ", " ".join(lines)).strip()


def evidence_in_diff(evidence: str, diff: str) -> bool:
    """Whether `evidence` is an excerpt of `diff`'s content (D5).

    Only content lines count (preprocessing.diff_body_lines: inside hunks, without the +, -
    or space marker), never file or hunk headers. Each evidence line loses a leading + or -,
    and whitespace runs collapse to one space on both sides, so indentation and a span
    across lines still match. The evidence may quote the diff raw or as the tools show it
    (our block tags escaped, prompts.escape_tags). Evidence shorter than
    MIN_EVIDENCE_CHARS after normalising proves nothing and is never found.
    """
    quoted = _normalise([line[1:] if line[:1] in "+-" else line for line in evidence.split("\n")])
    if len(quoted) < config.MIN_EVIDENCE_CHARS:
        return False
    body = diff_body_lines(diff)
    return any(quoted in _normalise(lines) for lines in (body, [prompts.escape_tags(line) for line in body]))


def apply_guards(risk: RiskFindings, pr: PRInput) -> tuple[RiskFindings, list[str]]:
    """Check every finding against the PR (§7), returning new findings and trace lines.

    - A finding on a file not in the PR (paths normalised) is dropped.
    - A kept finding gets the PR's spelling of the path.
    - Evidence found in that file's own diff (D5) → verified. Otherwise it stays unverified
      and its severity drops one level (low stays low); finalize lowers confidence for it.
    Only `verified` from here counts: the model can't set it (Finding drops it on input).
    The trace names paths only, never evidence, which could quote a secret.
    """
    files = changed_files_by_path(pr)
    kept, dropped, unverified = [], [], []
    for finding in risk.findings:
        file = files.get(normalize_path(finding.file))
        if file is None:
            dropped.append(finding.file)
            continue
        if evidence_in_diff(finding.evidence, file.diff):
            kept.append(finding.model_copy(update={"file": file.path, "verified": True}))
        else:
            unverified.append(file.path)
            kept.append(finding.model_copy(update={
                "file": file.path, "verified": False, "severity": _DEMOTED[finding.severity],
            }))

    trace = []
    if dropped:
        trace.append(
            f"Risk guards: {len(dropped)} finding(s) dropped, naming a file not in the PR: "
            f"{describe_paths('file', dropped)}."
        )
    if unverified:
        trace.append(
            f"Risk guards: {len(unverified)} finding(s) with evidence not found in their file's diff, "
            f"marked unverified and lowered one severity level: {describe_paths('file', unverified)}."
        )
    return RiskFindings(findings=kept), trace


if __name__ == "__main__":
    # Built with the real model class; nothing is sent, so no API key is needed.
    graph = build_risk_agent(llm.agent_model(config.EFFORT_RISK_AGENT), max_tool_calls=8, max_model_calls=10)
    print(graph.get_graph().draw_mermaid())
