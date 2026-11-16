"""Simple tests of the pipeline: the deterministic layer, the graph's paths (including the
validation retry), and the repair of a draft that still fails validation.

The assignment asks for no extensive test suite ("one simple test is fine"), so this module
has three small parametrized tests over a few representative PRs. No API key, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_workflow import config, nodes
from agent_workflow.graph import build_graph
from agent_workflow.preprocessing import load_core_patterns, preprocess
from agent_workflow.router import apply_hard_rules, decide_route
from agent_workflow.schemas import FocusArea, PRInput, ReviewOutput
from agent_workflow.scoring import complexity_score, fallback_review, repair_draft

ROOT = Path(__file__).resolve().parents[1]
CORE_PATTERNS = load_core_patterns(config.PROJECT_CONTEXT_PATH.read_text(encoding="utf-8"))
FAKE_SECRET = "s3cr3t-value-123"


def _file(path: str, added: list[str], removed: tuple[str, ...] | list[str] = ()) -> dict:
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return {
        "path": path,
        "status": "modified",
        "diff": f"@@ -1,{len(removed)} +1,{len(added)} @@\n{body}",
        "lines_added": len(added),
        "lines_removed": len(removed),
    }


def _pr(*files: dict) -> PRInput:
    return PRInput(pr_title="t", pr_description="d", author="a", changed_files=list(files))


def _sample() -> PRInput:
    return PRInput.model_validate_json((ROOT / "pr_input.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("pr", "route", "complexity", "max_tool_calls", "must_review"),
    [
        pytest.param(
            _sample(), "complex", 5.5, 14,
            ["src/flask/app.py", "src/flask/rate_limiter.py", "tests/test_rate_limit.py"],
            id="sample PR: core file + new module",
        ),
        pytest.param(
            _pr(_file("README.md", ["fix"], ["fxi"]), _file("docs/config.rst", ["x"])),
            "simple", 1.0, 8, ["README.md", "docs/config.rst"],
            id="docs only",
        ),
        pytest.param(
            _pr(_file("src/flask/debughelpers.py", ["# fix typo"], ["# fix tpyo"])),
            None, 1.5, 10, ["src/flask/debughelpers.py"],
            id="one-line comment fix: uncertain middle",
        ),
        pytest.param(
            _pr(*[_file(f"src/m{i:02}.py", ["x"] * 10) for i in range(60)]),
            "complex", 6.0, 30, [f"src/m{i:02}.py" for i in range(5)],
            id="60 source files: size rule, capped budget",
        ),
        pytest.param(
            _pr(_file("README.md", [f'SECRET_KEY = "{FAKE_SECRET}"'])),
            "complex", 1.0, 8, ["README.md"],
            id="secret in a docs file",
        ),
    ],
)
def test_deterministic_pipeline(
    pr: PRInput,
    route: str | None,
    complexity: float,
    max_tool_calls: int,
    must_review: list[str],
) -> None:
    result = preprocess(pr, CORE_PATTERNS)
    decision = apply_hard_rules(result.classifications, result.stats)

    assert (decision.route if decision else None) == route
    assert decision is None or decision.source == "rule"
    assert complexity_score(result.classifications) == complexity
    assert result.plan.max_tool_calls == max_tool_calls
    assert result.plan.must_review == must_review

    # The secret's text is never stored anywhere the review or the trace can reach.
    outputs = [
        *(c.model_dump_json() for c in result.classifications.values()),
        result.stats.model_dump_json(),
        result.plan.model_dump_json(),
        *result.notes,
        decision.reason if decision else "",
    ]
    assert not any(FAKE_SECRET in text for text in outputs)


@pytest.mark.parametrize(
    ("raw_pr", "route", "analysts_ran", "complexity", "failing_draft"),
    [
        pytest.param(
            _pr(_file("README.md", ["fix"], ["fxi"]), _file("docs/config.rst", ["x"])).model_dump(),
            "simple", False, 1.0, False,
            id="docs only: simple path",
        ),
        pytest.param(
            json.loads((ROOT / "pr_input.json").read_text(encoding="utf-8")),
            "complex", True, 5.5, False,
            id="sample PR: complex path, parallel analysts",
        ),
        pytest.param(
            json.loads((ROOT / "pr_input.json").read_text(encoding="utf-8")),
            "complex", True, 5.5, True,
            id="sample PR: draft still failing after the retry, repaired",
        ),
    ],
)
def test_graph_paths(
    raw_pr: dict,
    route: str,
    analysts_ran: bool,
    complexity: float,
    failing_draft: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Only behaviour that holds once the LLM nodes are real is asserted. From Phase 4 on,
    # the LLM nodes are patched on `agent_workflow.nodes` before build_graph(), so no API key.
    if failing_draft:  # every draft names a file not in the PR, so validation fails twice
        real_synthesize = nodes.synthesize

        def synthesize(state: dict) -> dict:
            update = real_synthesize(state)
            bad = [FocusArea(file="nope.py", concern="c")]
            update["draft"] = update["draft"].model_copy(update={"review_focus_areas": bad})
            return update

        monkeypatch.setattr(nodes, "synthesize", synthesize)
    graph = build_graph()
    ran = [node for update in graph.stream({"raw_pr": raw_pr}, stream_mode="updates") for node in update]
    result = graph.invoke({"raw_pr": raw_pr})

    assert ran.count("architecture_analyst") == ran.count("risk_agent") == int(analysts_ran)
    assert ran.count("synthesize") == ran.count("validate") == 1 + failing_draft  # at most 1 retry
    assert ran[-1] == "finalize"

    assert set(result) == {"output"}  # the output schema hides the internal state
    output = result["output"]
    assert isinstance(output, ReviewOutput)
    assert output.complexity_score == complexity
    trace = output.reasoning_trace
    assert all(line.startswith(f"Step {i}: ") for i, line in enumerate(trace, start=1))
    assert any(f": Route: {route} (rule: " in line for line in trace)
    if failing_draft:
        assert output.confidence <= 0.5
        assert any(": Repair: " in line for line in trace)
        assert not any("nope.py" in area for area in output.review_focus_areas)


@pytest.mark.parametrize(
    ("secret_path", "focus_file", "kept", "risk_level", "repairs"),
    [
        pytest.param("README.md", "README.md", ["README.md"], "high", 1, id="secret outside tests: raised to high"),
        pytest.param(
            "tests/test_app.py", "tests/test_app.py", ["tests/test_app.py"], "low", 0,
            id="secret in a test file: not forced (2.7)",
        ),
        pytest.param(
            "README.md", "nope.py", ["README.md"], "high", 3,
            id="only focus area names a file not in the PR: dropped, rule-based areas instead",
        ),
    ],
)
def test_repair_failed_draft(
    secret_path: str, focus_file: str, kept: list[str], risk_level: str, repairs: int
) -> None:
    """A draft still failing after its retry: finalize repairs what rules can fix (§8)."""
    result = preprocess(_pr(_file(secret_path, [f'SECRET_KEY = "{FAKE_SECRET}"'])), CORE_PATTERNS)
    draft = fallback_review(result.classifications, result.plan, result.stats).model_copy(
        update={"risk_level": "low", "review_focus_areas": [FocusArea(file=focus_file, concern="c")]}
    )
    repaired, trace = repair_draft(
        draft,
        classifications=result.classifications,
        route=decide_route(result.classifications, result.stats),
        plan=result.plan,
        risk=None,
        scan_hits=result.stats.scan_hits,
    )

    assert repaired.risk_level == risk_level
    # A raised level is explained at the start of the reasoning, not only in the trace.
    assert repaired.risk_reasoning.startswith("[Repaired: ") == (risk_level != "low")
    assert [a.file for a in repaired.review_focus_areas] == kept
    assert len(trace) == repairs
    assert all(line.startswith("Repair: ") and FAKE_SECRET not in line for line in trace)
