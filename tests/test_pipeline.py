"""One simple test of the deterministic layer: preprocess → hard routing rules → complexity.

The assignment asks for no extensive test suite ("one simple test is fine"), so this is a
single parametrized test over a few representative PRs. No API key and no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_workflow import config
from agent_workflow.preprocessing import load_core_patterns, preprocess
from agent_workflow.router import apply_hard_rules
from agent_workflow.schemas import PRInput
from agent_workflow.scoring import complexity_score

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
