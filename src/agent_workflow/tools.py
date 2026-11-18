"""The risk agent's 4 read-only tools (docs/PLAN.md §7).

Each tool is a pure function over `RiskAgentContext` (tested directly), wrapped by a thin
`@tool` with a Pydantic argument schema. The context is injected through `ToolRuntime`, so
the model never sees it in the tool schema, and there are no globals.

Every tool is bounded:
- every result, error messages included, is at most `TOOL_RESULT_MAX_CHARS` characters, so
  one run's executed tool output is at most the tool budget × that (`TOOL_OUTPUT_TOTAL_MAX_CHARS`),
- a diff page ends after `TOOL_DIFF_PAGE_LINES` lines or at `TOOL_PAGE_MAX_CHARS` characters,
  each line cut to `TOOL_LINE_MAX_CHARS`; page 1 lists at most `TOOL_MAX_HUNK_HEADERS` hunk
  headers with their pages,
- searches stop at `SEARCH_DIFF_MAX_HITS` hits of at most `SEARCH_HIT_MAX_CHARS` characters,
  and after `SEARCH_DIFF_TIMEOUT_SECONDS` of matching: the pattern is model-written and runs
  over untrusted PR text, so the `regex` package's timeout stops catastrophic backtracking
  (Python's `re` has none). Patterns that would be expensive to compile are rejected first.
Tools only read the PR: no file system, no network. Their output, error messages included,
is untrusted PR text, so our block tags are escaped (prompts.escape_tags).

A mistake the agent can fix raises `ToolInputError`; `on_tool_error` (for
`ToolErrorMiddleware`) turns it, and any other exception, into an error `ToolMessage`
the agent can act on (§7, "Tool error handling").
"""

from __future__ import annotations

import bisect
import difflib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

import regex
from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from . import config, prompts
from .preprocessing import changed_files_by_path, cut_text, hunk_headers, normalize_path, review_order
from .schemas import ChangedFile, FileClassification, PRInput


@dataclass(frozen=True)
class RiskAgentContext:
    """The read-only PR context the tools read (§6). Passed with the run (`context=` on the
    agent's stream); never in the agent's state or the model's tool schema.

    Defined here, next to the tools that read it, rather than in risk_agent.py: the
    `ToolRuntime[RiskAgentContext]` annotations must resolve when the tools are created,
    and risk_agent.py imports this module.
    """

    pr: PRInput
    classifications: Mapping[str, FileClassification]
    project_context: str


class ToolInputError(Exception):
    """A tool call the agent can fix; the message says how."""


class DiffPage(NamedTuple):
    """What read_file_diff returns: the PR's spelling of the path, the file's page count
    (so the agent's state can tell a full read from a partial one, §6), and the output."""

    path: str
    pages: int
    text: str


# ---------------------------------------------------------------------------
# Pure functions (one per tool)
# ---------------------------------------------------------------------------


def _changed_file(ctx: RiskAgentContext, path: str) -> ChangedFile:
    """The changed file `path` names (the shared path rule), any file in the PR."""
    files = changed_files_by_path(ctx.pr)
    wanted = normalize_path(path)
    found = files.get(wanted)
    if found is not None:
        return found
    close = difflib.get_close_matches(wanted, list(files), n=config.TOOL_PATH_SUGGESTIONS, cutoff=0.5)
    hint = (
        "closest changed files: " + ", ".join(f"`{files[p].path}`" for p in close)
        if close
        else "call list_changed_files to see them"
    )
    raise ToolInputError(f"unknown path `{path}`; {hint}")


def _diff_lines(diff: str) -> list[str]:
    """The diff's lines, split on "\\n" only, like preprocessing._split_lines (so the line
    numbers match hunk_headers). Unlike it, a final newline doesn't add an empty last line,
    and an empty diff has no lines."""
    return diff.removesuffix("\n").split("\n") if diff else []


def _bounded(text: str) -> str:
    """Every tool result's final cap (TOOL_RESULT_MAX_CHARS). The tools stay under it by
    design, so this only cuts pathological input, such as a path thousands of characters long."""
    limit = config.TOOL_RESULT_MAX_CHARS
    if len(text) <= limit:
        return text
    note = f"… [result cut at {limit} characters]"
    return text[: limit - len(note)] + note


def _joined_within(lines: list[str], what: str) -> str:
    """`lines` joined by newlines, stopping before TOOL_RESULT_MAX_CHARS with a note of how
    many were left out, so a long list ends on a whole line."""
    room = config.TOOL_RESULT_MAX_CHARS - 100  # left for the note
    kept: list[str] = []
    size = 0
    for line in lines:
        size += len(prompts.escape_tags(line)) + 1
        if size > room:
            break
        kept.append(line)
    if len(kept) < len(lines):
        kept.append(f"… {len(lines) - len(kept)} more {what} not shown")
    return "\n".join(kept)


class _Paging(NamedTuple):
    """How read_file_diff splits a diff into pages; search_diff uses the same page numbers."""

    lines: list[str]  # the diff's lines as shown: each cut to TOOL_LINE_MAX_CHARS
    starts: list[int]  # the 0-based index of each page's first line
    headers: list[tuple[int, str]]  # (1-based diff line, hunk header)

    def page_of(self, line_number: int) -> int:
        """The page holding the 1-based diff line `line_number`."""
        return bisect.bisect_right(self.starts, line_number - 1)


def _header_list(paging: _Paging, page_of: Callable[[int], int]) -> str:
    """Page 1's list of hunk headers, with each one's diff line and page (§3.5)."""
    shown = paging.headers[: config.TOOL_MAX_HUNK_HEADERS]
    listed = [f"line {n} (page {page_of(n)}): {cut_text(h, config.TOOL_HUNK_HEADER_MAX_CHARS)}" for n, h in shown]
    if len(paging.headers) > len(shown):
        listed.append(f"+{len(paging.headers) - len(shown)} more")
    return "; ".join(listed) if listed else "none"


def _paging(diff: str) -> _Paging:
    """Split a diff into pages (§3.5). A page ends after TOOL_DIFF_PAGE_LINES lines, or
    before its shown text would pass TOOL_PAGE_MAX_CHARS; page 1 also holds the header list,
    so its room is smaller by that list's longest possible length. A page always holds at
    least one line, so every line is on some page."""
    lines = [cut_text(line, config.TOOL_LINE_MAX_CHARS) for line in _diff_lines(diff)]
    paging = _Paging(lines, [0], hunk_headers(diff))
    # Page numbers aren't known yet: measure the header list with the widest one possible.
    widest = int("9" * len(str(max(1, len(lines)))))
    room = config.TOOL_PAGE_MAX_CHARS - len(prompts.escape_tags(_header_list(paging, lambda _: widest)))
    size = 0
    for i, line in enumerate(lines):
        cost = len(prompts.escape_tags(line)) + 1
        if i > paging.starts[-1] and (
            i - paging.starts[-1] == config.TOOL_DIFF_PAGE_LINES or size + cost > room
        ):
            paging.starts.append(i)
            size, room = 0, config.TOOL_PAGE_MAX_CHARS
        size += cost
    return paging


def list_changed_files(ctx: RiskAgentContext) -> str:
    """One line per changed file: path, status, category, +/-, priority, and the filter
    reason for mechanical files (§7)."""
    lines = []
    for c in review_order(ctx.classifications):
        mechanical = f", mechanical: {c.filter_reason}" if c.filter_reason is not None else ""
        lines.append(
            f"- {c.path} ({c.status}, {c.category}, +{c.lines_added}/-{c.lines_removed}, "
            f"priority {c.priority:.2f}{mechanical})"
        )
    return _bounded(prompts.escape_tags(_joined_within(lines, "file(s)")))


def read_file_diff(ctx: RiskAgentContext, path: str, page: int = 1) -> DiffPage:
    """One page of a file's diff (§3.5). Page 1 also lists the hunk headers, with their diff
    line numbers and pages; later pages show their own `@@` lines in the text.

    Any changed file can be read, mechanical ones included: a filtered file with a scan hit
    is in must_review. Every shown line is cut to TOOL_LINE_MAX_CHARS; quoting a cut line's
    visible part still matches the diff.
    """
    file = _changed_file(ctx, path)
    paging = _paging(file.diff)
    pages = len(paging.starts)
    if not 1 <= page <= pages:
        raise ToolInputError(
            f"page {page} is out of range: `{file.path}` has {pages} page(s) (1–{pages})"
        )
    start = paging.starts[page - 1]
    end = paging.starts[page] if page < pages else len(paging.lines)
    chunk = paging.lines[start:end]

    if page == 1:
        header_text = _header_list(paging, paging.page_of)
    else:
        header_text = "listed on page 1" if paging.headers else "none"
    if chunk:
        span = f"diff lines {start + 1}–{end} of {len(paging.lines)}"
        body = "\n".join(chunk)
    else:
        span, body = "empty diff", "(no diff text)"
    text = f"Page {page} of {pages} ({span}). Hunk headers: {header_text}\n{body}"
    return DiffPage(file.path, pages, _bounded(prompts.tool_diff_block(file.path, page, pages, text)))


_COUNTED_REPEAT = re.compile(r"\{(\d*),?(\d*)\}")


def _check_pattern(pattern: str) -> None:
    """Reject a pattern that could be expensive to compile, before compiling it (the regex
    timeout covers only the matching). Counting every {n} / {m,n} as if nested over-counts
    side-by-side repeats, which is the safe side."""
    if len(pattern) > config.SEARCH_PATTERN_MAX_CHARS:
        raise ToolInputError(
            f"the pattern is longer than {config.SEARCH_PATTERN_MAX_CHARS} characters; "
            "search for a shorter, distinctive part"
        )
    product = 1
    for low, high in _COUNTED_REPEAT.findall(pattern):
        product *= max(int(low or 0), int(high or 0), 1)
    if product > config.SEARCH_REPEAT_PRODUCT_MAX:
        raise ToolInputError(
            f"the pattern's counted repeats ({{n}} or {{m,n}}) multiply to more than "
            f"{config.SEARCH_REPEAT_PRODUCT_MAX}; use smaller counts, or + and *"
        )


def _search_timeout() -> ToolInputError:
    return ToolInputError(
        f"the search took longer than {config.SEARCH_DIFF_TIMEOUT_SECONDS:g}s and was "
        "stopped; use a simpler pattern, without nested or overlapping repeats such as (a|aa)+"
    )


def search_diff(ctx: RiskAgentContext, pattern: str) -> str:
    """Regex hits across every changed file's diff, in review order, as
    `path:N (page P): text`, where N is the line number within that file's diff and P the
    read_file_diff page holding it (§7). Lines are matched as they appear in the diff, with
    their +, - or space marker.

    A pattern that would be expensive to compile is rejected first. The matching stops after
    SEARCH_DIFF_TIMEOUT_SECONDS in total, and the agent is asked for a simpler pattern: a
    pathological one (e.g. `(a|aa)+$`) can't hang the review.
    """
    _check_pattern(pattern)
    try:
        compiled = regex.compile(pattern)
    except regex.error as err:
        raise ToolInputError(
            f"invalid regex: {err}; escape special characters such as ( [ . * with a backslash"
        ) from err

    deadline = time.monotonic() + config.SEARCH_DIFF_TIMEOUT_SECONDS
    files = changed_files_by_path(ctx.pr)
    hits: list[str] = []
    total = 0
    for c in review_order(ctx.classifications):
        diff = files[normalize_path(c.path)].diff
        paging: _Paging | None = None  # only built for a file with a hit
        for n, line in enumerate(_diff_lines(diff), start=1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _search_timeout()
            try:
                found = compiled.search(line, timeout=remaining)
            except TimeoutError as err:
                raise _search_timeout() from err
            if found:
                total += 1
                if len(hits) < config.SEARCH_DIFF_MAX_HITS:
                    paging = paging or _paging(diff)
                    hits.append(f"{c.path}:{n} (page {paging.page_of(n)}): {cut_text(line, config.SEARCH_HIT_MAX_CHARS)}")
    if not hits:
        return "no matches"
    if total > len(hits):
        hits.append(f"… {total - len(hits)} more hit(s) not shown; narrow the pattern")
    return _bounded(prompts.escape_tags(_joined_within(hits, "line(s)")))


_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def read_project_context(ctx: RiskAgentContext, topic: str) -> str:
    """The first `##` section of project_context.md whose heading contains `topic`
    (ignoring case). A blank topic is an error, not a match for every heading."""
    headings = list(_SECTION_HEADING.finditer(ctx.project_context))
    wanted = topic.strip().lower()
    for i, heading in enumerate(headings):
        if wanted and wanted in heading.group(1).lower():
            end = headings[i + 1].start() if i + 1 < len(headings) else len(ctx.project_context)
            return _bounded(ctx.project_context[heading.start() : end].strip())
    available = ", ".join(f"`{h.group(1)}`" for h in headings)
    problem = "the topic is blank" if not wanted else f"no section matches `{topic}`"
    raise ToolInputError(f"{problem}; available topics: {available}")


# ---------------------------------------------------------------------------
# The @tool wrappers the agent sees. Their descriptions are built from config, so the limits
# the model is told match the ones the tools apply.
# ---------------------------------------------------------------------------


class _ReadFileDiffArgs(BaseModel):
    path: str = Field(description="A changed file's path, as list_changed_files shows it.")
    page: int = Field(1, ge=1, description="Page number, from 1; the output says how many pages there are.")


class _SearchDiffArgs(BaseModel):
    pattern: str = Field(
        min_length=1,
        description="A Python regular expression, matched against each diff line (marker included).",
    )


class _ReadProjectContextArgs(BaseModel):
    topic: str = Field(min_length=1, description="A word from a section heading, e.g. 'security' or 'concurrency'.")


# No args_schema: an explicit empty schema stops the runtime from being injected (Phase 5.5).
@tool(
    "list_changed_files",
    description=(
        "List every changed file: path, status, category, lines added/removed, review "
        "priority, and whether it was skipped as mechanical (lockfile, formatting-only, ...)."
    ),
)
def list_changed_files_tool(runtime: ToolRuntime[RiskAgentContext]) -> str:
    return list_changed_files(runtime.context)


@tool(
    "read_file_diff",
    args_schema=_ReadFileDiffArgs,
    description=(
        f"Read one changed file's diff, in pages of at most {config.TOOL_DIFF_PAGE_LINES} lines "
        f"or {config.TOOL_PAGE_MAX_CHARS:,} characters. Page 1 lists the hunk headers (which name "
        "the enclosing function or class) with the page each is on. A file counts as read only "
        "once every page has been read."
    ),
)
def read_file_diff_tool(path: str, runtime: ToolRuntime[RiskAgentContext], page: int = 1) -> Command:
    result = read_file_diff(runtime.context, path, page)
    # Record the page in the agent's state (pages_read, §6) along with the result.
    return Command(update={
        "pages_read": [(result.path, page, result.pages)],
        "messages": [ToolMessage(result.text, tool_call_id=runtime.tool_call_id, name="read_file_diff")],
    })


@tool(
    "search_diff",
    args_schema=_SearchDiffArgs,
    description=(
        "Search every changed file's diff with a regular expression. Returns up to "
        f"{config.SEARCH_DIFF_MAX_HITS} hits as `path:line (page P): text`, where line is the "
        "line number within that file's diff. Lines keep their diff marker (+, - or a space) "
        "and hunk headers are lines too, so `^def` matches nothing: write `^[+ -]def`, or "
        "leave out the `^`. Keep patterns simple: at most "
        f"{config.SEARCH_PATTERN_MAX_CHARS} characters, small {{n}} counts, and a search "
        f"running over {config.SEARCH_DIFF_TIMEOUT_SECONDS:g}s is stopped."
    ),
)
def search_diff_tool(pattern: str, runtime: ToolRuntime[RiskAgentContext]) -> str:
    return search_diff(runtime.context, pattern)


@tool(
    "read_project_context",
    args_schema=_ReadProjectContextArgs,
    description=(
        "Read one section of the project's conventions (architecture, security, concurrency "
        "and state, public API and compatibility, testing, ...) by a word from its heading."
    ),
)
def read_project_context_tool(topic: str, runtime: ToolRuntime[RiskAgentContext]) -> str:
    return read_project_context(runtime.context, topic)


RISK_TOOLS = [list_changed_files_tool, read_file_diff_tool, search_diff_tool, read_project_context_tool]


def on_tool_error(exc: Exception, request: Any) -> str:
    """ToolErrorMiddleware's handler: every tool exception becomes an error ToolMessage the
    agent can act on, with no stack trace (§7). The message can quote PR paths (the closest
    matches), so it's escaped like all tool output. LangGraph's control-flow signals never
    reach it. `request` (a ToolCallRequest) isn't needed."""
    if isinstance(exc, ToolInputError):
        return _bounded(prompts.escape_tags(f"Error: {exc}"))
    return _bounded(prompts.escape_tags(f"Error: {type(exc).__name__}: {exc}"))
