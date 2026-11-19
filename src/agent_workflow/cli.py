"""The command-line interface (docs/PLAN.md §9).

    uv run python analyze_pr.py --pr pr_input.json --output code_review_output.json

It checks the input, runs the graph (traced in Langfuse when configured), writes the review
atomically, and returns the §9 exit codes:
- 0: a full review was written,
- 2: invalid input; nothing written,
- 3: a degraded review was written (a stage failed, and the trace says which). When it's
  the rule-based fallback review, the stderr summary says so,
- 1: a fatal error (a bug, a missing or rejected API key, or an unwritable output path).
  Nothing is written, except that a finished review whose file can't be written is printed
  to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from pydantic import ValidationError

from . import config
from .graph import build_graph
from .observability import traced_run
from .preprocessing import first_line
from .schemas import PRInput, ReviewOutput

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_INVALID_INPUT = 2
EXIT_DEGRADED = 3


# An error's message on stderr: its first line only, cut like the trace's "Degraded:" lines.
_MESSAGE_MAX_CHARS = config.DEGRADED_MESSAGE_MAX_CHARS


class InputError(Exception):
    """The PR file can't be used. The message is shown as-is, then the CLI exits 2."""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def load_pr(path: Path) -> dict[str, Any]:
    """The PR file's JSON object, checked against PRInput (§9 "Input and parsing").

    Raises InputError on a file that is too large, unreadable, not UTF-8, not JSON, or not
    in the expected format. At most MAX_INPUT_BYTES + 1 bytes are read, so a huge file, a
    pipe or a device (`/dev/zero`) is never loaded whole: a size from stat() wouldn't bound
    those (7.11, CR4). Validation errors are listed in full, by field path and message only: an input
    value can be a whole diff. The raw dict is returned, because it's the graph's input
    (parse_pr validates it again, which can't fail now).
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(config.MAX_INPUT_BYTES + 1)
    except OSError as err:
        raise InputError(f"cannot read PR file '{path}': {err.strerror or err}.") from err
    if len(data) > config.MAX_INPUT_BYTES:
        raise InputError(f"PR file '{path}' is over the size limit ({config.MAX_INPUT_BYTES:,} bytes).")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise InputError(f"PR file '{path}' is not valid UTF-8 ({err.reason} at byte {err.start}).") from err

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as err:
        raise InputError(
            f"PR file '{path}' is not valid JSON: {err.msg} (line {err.lineno}, column {err.colno})."
        ) from err
    except ValueError as err:  # e.g. an integer over Python's 4,300-digit limit (CR2)
        raise InputError(f"PR file '{path}' is not valid JSON: {first_line(err, _MESSAGE_MAX_CHARS)}.") from err
    except RecursionError as err:  # nested deeper than the parser's recursion limit (CR2)
        raise InputError(f"PR file '{path}' is not valid JSON: it's nested too deeply.") from err

    try:
        PRInput.model_validate(raw)
    except ValidationError as err:
        lines = [f"  {'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in err.errors()]
        count = f"{len(lines)} error{'' if len(lines) == 1 else 's'}"
        raise InputError(
            f"PR file '{path}' doesn't match the expected format ({count}):\n" + "\n".join(lines)
        ) from err
    return raw


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _output_mode(path: Path) -> int:
    """The permissions the review gets: an existing file's own, or a new file's usual ones
    (0o666 minus the umask). The temp file is created 0o600, and the rename would keep that
    (CR3)."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0)  # reading the umask means setting it; restored at once
        os.umask(umask)
        return 0o666 & ~umask


def write_output(output: ReviewOutput, path: Path) -> None:
    """Write the review atomically (§9): a temp file in the same folder, then a rename, so
    an existing file at `path` is never left half-written. Missing folders are created.
    Raises OSError; the temp file is removed first. A symlink at `path` is replaced by the
    file, not followed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(output.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fchmod(handle.fileno(), _output_mode(path))
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="analyze_pr.py",
        description="Review a pull request given as JSON and write a structured review.",
    )
    parser.add_argument("--pr", required=True, type=Path, help="the PR JSON file")
    parser.add_argument(
        "--output", type=Path, default=Path("code_review_output.json"),
        help="where to write the review (default: code_review_output.json)",
    )
    return parser.parse_args(argv)  # a usage error exits 2, like any invalid input


# Said on stderr when no model's review was available: the JSON says it only in its text.
FALLBACK_NOTICE = "This is a rule-based fallback review, not an AI-generated review."


def _summary(output: ReviewOutput, path: Path, degraded: list[str], fallback: bool) -> str:
    line = (
        f"Review written to {path}: risk {output.risk_level}, confidence {output.confidence:.2f}, "
        f"complexity {output.complexity_score}."
    )
    if degraded:
        line += f" Degraded: {', '.join(degraded)}."
    return f"{line} {FALLBACK_NOTICE}" if fallback else line


def main(argv: list[str] | None = None) -> int:
    """Run one review; returns the exit code (see the module docstring)."""
    args = _parse_args(argv)
    # The .env of the folder the CLI runs in (or a parent), not of the installed package (CR6).
    load_dotenv(find_dotenv(usecwd=True))

    try:
        raw_pr = load_pr(args.pr)
    except InputError as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_INVALID_INPUT

    # Every path needs Claude: without a key, the run could only write a review no model
    # wrote (D8). A rejected key fails the run below for the same reason (llm.is_degradable).
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        print("Error: ANTHROPIC_API_KEY is not set (see .env.example).", file=sys.stderr)
        return EXIT_FATAL

    try:
        with traced_run("pr-review", metadata={"pr_file": args.pr.name}) as run_config:
            result = build_graph().invoke({"raw_pr": raw_pr}, config=run_config)
            if run_config.get("callbacks"):  # what actually happened, not just the keys
                print("Langfuse tracing: on.", file=sys.stderr)
    except Exception as err:  # a bug or a fatal API error: the node handlers re-raised it
        print(f"Fatal error: {type(err).__name__}: {first_line(err, _MESSAGE_MAX_CHARS)}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return EXIT_FATAL

    output: ReviewOutput = result["output"]
    degraded: list[str] = result["degraded"]
    fallback: bool = result["fallback"]
    try:
        write_output(output, args.output)
    except OSError as err:
        print(
            f"Error: cannot write '{args.output}': {err.strerror or err}; the review is printed to stdout instead.",
            file=sys.stderr,
        )
        print(output.model_dump_json(indent=2))
        return EXIT_FATAL

    print(_summary(output, args.output, degraded, fallback), file=sys.stderr)
    return EXIT_DEGRADED if degraded else EXIT_OK


def run() -> None:
    """The script entry point (`analyze-pr`, and `analyze_pr.py`)."""
    sys.exit(main())
