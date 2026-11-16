"""The review graph (docs/PLAN.md §2).

    START → parse_pr → classify_pr ─┬─ simple ──────────────────────────────┐
                                    │                                       ▼
                                    └─ complex ─┬─► architecture_analyst ─► synthesize ⇄ validate → finalize → END
                                                └─► risk_agent ──────────►

The caller sees {raw_pr} → {output}; everything else is internal state. There is no
checkpointer: this is a one-shot CLI (§11). Print the diagram with:

    uv run python -m agent_workflow.graph
"""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from . import config, nodes
from .state import GraphInput, GraphOutput, ReviewState

ANALYSTS = ["architecture_analyst", "risk_agent"]


def after_classify(state: ReviewState) -> Literal["synthesize"] | list[str]:
    """Simple path: straight to synthesize. Complex path: both analysts, in parallel."""
    return "synthesize" if state["route"].route == "simple" else ANALYSTS


def after_validate(state: ReviewState) -> Literal["synthesize", "finalize"]:
    """Send a failing draft back to synthesize, at most MAX_VALIDATION_RETRIES times (§8)."""
    if state["validation_errors"] and state["revision_count"] < config.MAX_VALIDATION_RETRIES:
        return "synthesize"
    return "finalize"


def build_graph() -> CompiledStateGraph:
    """Wire and compile the 7 nodes.

    Nodes are looked up on the `nodes` module when the graph is built, so a check or test
    can swap an LLM node by patching `nodes.<name>` first. RetryPolicy on the LLM nodes is
    added in Phase 4, once the exception raised on a schema mismatch has been verified.
    """
    graph = StateGraph(ReviewState, input_schema=GraphInput, output_schema=GraphOutput)
    for name in (
        "parse_pr",
        "classify_pr",
        "architecture_analyst",
        "risk_agent",
        "synthesize",
        "validate",
        "finalize",
    ):
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "parse_pr")
    graph.add_edge("parse_pr", "classify_pr")
    graph.add_conditional_edges("classify_pr", after_classify, ["synthesize", *ANALYSTS])
    graph.add_edge(ANALYSTS, "synthesize")  # the join: runs once, after both analysts
    graph.add_edge("synthesize", "validate")
    graph.add_conditional_edges("validate", after_validate, ["synthesize", "finalize"])
    graph.add_edge("finalize", END)
    return graph.compile()


if __name__ == "__main__":
    print(build_graph().get_graph().draw_mermaid())
