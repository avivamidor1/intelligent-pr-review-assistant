"""Review a pull request given as JSON (docs/PLAN.md §9).

    uv run python analyze_pr.py --pr pr_input.json --output code_review_output.json

A thin wrapper: the CLI lives in `agent_workflow.cli`, so it can be tested like the rest of
the package. Exit codes: 0 full review, 3 degraded review, 2 invalid input, 1 fatal error.
"""

from agent_workflow.cli import run

if __name__ == "__main__":
    run()
