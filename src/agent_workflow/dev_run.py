"""Development runner: run the graph on a PR JSON file, traced in Langfuse when configured.

    uv run python -m agent_workflow.dev_run [path/to/pr.json]   # default: pr_input.json

Dev only. The real CLI (`analyze_pr.py`: input checks, exit codes, atomic write) is built in
Phase 7 and reuses `observability.traced_run`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from .graph import build_graph
from .observability import langfuse_enabled, traced_run


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0] if args else "pr_input.json")
    raw_pr = json.loads(path.read_text(encoding="utf-8"))

    with traced_run("pr-review", metadata={"pr_file": path.name}) as config:
        result = build_graph().invoke({"raw_pr": raw_pr}, config=config)

    print(result["output"].model_dump_json(indent=2))
    print(f"Langfuse tracing: {'on' if langfuse_enabled() else 'off'}", file=sys.stderr)


if __name__ == "__main__":
    main()
