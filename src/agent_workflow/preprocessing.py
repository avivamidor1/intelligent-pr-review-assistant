"""Deterministic preprocessing for `parse_pr`.

Design: docs/PLAN.md §3.1–3.2, §9. Functions are pure: they take explicit arguments,
return new objects and do no I/O.
"""

from __future__ import annotations

import keyword
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import cache
from pathlib import PurePosixPath
from typing import Literal

from . import config
from .schemas import (
    ChangedFile,
    FileCategory,
    FileClassification,
    FileStatus,
    PRInput,
    PRStats,
    ReviewPlan,
    ScanHit,
)

# ---------------------------------------------------------------------------
# project_context.md
# ---------------------------------------------------------------------------

_CORE_HEADING = re.compile(r"^##\s+Core paths\s*$", re.IGNORECASE)
_NEXT_SECTION = re.compile(r"^#{1,2}\s")
# A bullet whose whole content is one backticked glob, e.g. "- `src/flask/app.py`".
# Bullets with prose (explanations that mention `names`) are ignored.
_GLOB_BULLET = re.compile(r"^\s*[-*]\s+`([^`]+)`\s*$")


def load_core_patterns(markdown: str) -> list[str]:
    """Return the globs listed in the `## Core paths` section of project_context.md.

    Only bullets that consist of a single backticked glob are read, up to the next
    `#` or `##` heading. A missing or empty section means the project has no core files.
    """
    patterns: list[str] = []
    in_section = False
    for line in markdown.splitlines():
        if _CORE_HEADING.match(line):
            in_section = True
            continue
        if in_section and _NEXT_SECTION.match(line):
            break
        if in_section and (match := _GLOB_BULLET.match(line)):
            patterns.append(match.group(1).strip())
    return patterns


# ---------------------------------------------------------------------------
# Path matching (conventions documented in config.py)
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    """Strip a leading "./" or "/" so that patterns match however the path is written."""
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def changed_files_by_path(pr: PRInput) -> dict[str, ChangedFile]:
    """The PR's files by normalised path: the one rule for matching a path the model wrote
    (the agent's tools and the hallucination guards both use it)."""
    return {normalize_path(f.path): f for f in pr.changed_files}


def _matches_name(path: str, globs: Sequence[str]) -> bool:
    """fnmatch globs against the basename, case-insensitive (e.g. `*.md` matches `NOTES.MD`)."""
    name = normalize_path(path).rsplit("/", 1)[-1].lower()
    return any(fnmatchcase(name, glob.lower()) for glob in globs)


def _matches_dir(path: str, patterns: Sequence[str]) -> bool:
    """A "dir/" pattern matches at the start of the path or after any "/", case-insensitive."""
    wrapped = "/" + normalize_path(path).lower()
    return any("/" + pattern.lower() in wrapped for pattern in patterns)


def _matches_path(path: str, name_globs: Sequence[str], dir_patterns: Sequence[str]) -> bool:
    """The path matches one of the basename globs or one of the folder patterns."""
    return _matches_name(path, name_globs) or _matches_dir(path, dir_patterns)


@cache
def _core_glob_regex(glob: str) -> re.Pattern[str]:
    """Translate a core-path glob: `*` and `?` never cross a "/" (case-sensitive)."""
    parts = []
    for char in glob:
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
    return re.compile("".join(parts))


def _matches_core(path: str, core_patterns: Sequence[str]) -> bool:
    normalized = normalize_path(path)
    return any(_core_glob_regex(glob).fullmatch(normalized) for glob in core_patterns)


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_BINARY_MARKERS = tuple(re.compile(p, re.MULTILINE) for p in config.BINARY_MARKER_PATTERNS)


def _split_lines(text: str) -> list[str]:
    """Split on "\\n" only. str.splitlines() would also split on characters that are valid
    inside a source line (form feed, \\u2028, ...), turning one line into several."""
    return text.split("\n")


def _is_file_header(lines: list[str], i: int) -> bool:
    """A file header inside a diff: `diff --git ...`, or a `--- `/`+++ ` pair directly
    followed by an `@@` header. Such headers appear when diffs are joined (merged paths).

    A removed line "-- x" followed by an added line "++ y" and then a new hunk would look
    the same; that is accepted as a rare edge case.
    """
    line = lines[i]
    if line.startswith("diff --git "):
        return True
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    after = lines[i + 2] if i + 2 < len(lines) else ""
    prev = lines[i - 1] if i > 0 else ""
    if line.startswith("--- ") and nxt.startswith("+++ ") and _HUNK_HEADER.match(after):
        return True
    return (
        line.startswith("+++ ") and prev.startswith("--- ") and bool(_HUNK_HEADER.match(nxt))
    )


def _hunks(diff: str) -> list[list[str]] | None:
    """Split a diff into hunk bodies (the lines after each `@@` header).

    Lines before the first header are ignored. A file header (see `_is_file_header`) ends
    the current hunk, and lines after it are ignored until the next `@@` header.
    Returns None when the diff has no hunk.
    """
    lines = _split_lines(diff)
    hunks: list[list[str]] = []
    in_hunk = False
    for i, line in enumerate(lines):
        if _HUNK_HEADER.match(line):
            hunks.append([])
            in_hunk = True
        elif _is_file_header(lines, i):
            in_hunk = False
        elif in_hunk:
            hunks[-1].append(line)
    return hunks or None


def diff_body_lines(diff: str) -> list[str]:
    """The diff's content lines without their +, - or space marker: the lines inside hunks,
    never file or hunk headers. A diff with no hunk (malformed raw text) gives its lines as
    they are. Used to check quoted evidence (risk_agent.evidence_in_diff)."""
    hunks = _hunks(diff)
    if hunks is None:
        return _split_lines(diff)
    return [line[1:] for hunk in hunks for line in hunk]


def hunk_headers(diff: str) -> list[tuple[int, str]]:
    """Every `@@` header with its 1-based line number in the diff text (for the agent's tools)."""
    return [(n, line) for n, line in enumerate(_split_lines(diff), start=1) if _HUNK_HEADER.match(line)]


def hunks_text(diff: str) -> str:
    """The diff's hunks as text: each `@@` header followed by its lines, with file headers
    and anything outside a hunk left out (the same split as `_hunks`, which makes one hunk
    per `@@` header). "" when the diff has no hunk. Used for the Jev payload."""
    hunks = _hunks(diff)
    if hunks is None:
        return ""
    headers = [header for _, header in hunk_headers(diff)]
    lines: list[str] = []
    for header, body in zip(headers, hunks, strict=True):
        lines += [header, *body]
    return "\n".join(lines)


def _change_blocks(hunk: list[str]) -> list[tuple[list[str], list[str]]]:
    """Split a hunk into change blocks: runs of removed/added lines between context lines.

    Returns (removed, added) pairs, holding line contents without the +/- prefix.
    """
    blocks: list[tuple[list[str], list[str]]] = []
    removed: list[str] = []
    added: list[str] = []
    for line in hunk:
        if line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file" doesn't end a block
        else:  # any other line (context " ...", a blank line) ends the current block
            if removed or added:
                blocks.append((removed, added))
            removed, added = [], []
    if removed or added:
        blocks.append((removed, added))
    return blocks


def count_diff_lines(diff: str) -> tuple[int, int] | None:
    """Count (added, removed) lines inside hunks. None when the diff has no hunk.

    Only lines inside hunks count, so file headers (`+++ b/x`) are skipped, while a
    removed line whose content starts with "--" (shown as `---...`) is counted.
    """
    hunks = _hunks(diff)
    if hunks is None:
        return None
    added = sum(1 for hunk in hunks for line in hunk if line.startswith("+"))
    removed = sum(1 for hunk in hunks for line in hunk if line.startswith("-"))
    return added, removed


# ---------------------------------------------------------------------------
# Status, noise filter, category
# ---------------------------------------------------------------------------

_KNOWN_STATUSES: dict[str, FileStatus] = {
    "added": "added",
    "modified": "modified",
    "deleted": "deleted",
    "removed": "deleted",
    "renamed": "renamed",
}

# A complete Python import statement with nothing after it except a comment.
_IMPORT_LINE = re.compile(
    r"^\s*(import\s+[\w.]+(\s+as\s+\w+)?(\s*,\s*[\w.]+(\s+as\s+\w+)?)*"
    r"|from\s+[.\w]+\s+import\s+[\w\s,.*()]+?)\s*(#.*)?$"
)
_PYTHON_SUFFIXES = (".py", ".pyi")
_NOISE_REASON = "lockfile/generated/vendored"


def map_status(raw: str) -> tuple[FileStatus, bool]:
    """Map the raw status to a FileStatus. Returns (status, was_unknown).

    Case-insensitive and stripped; `removed` → `deleted`; anything unknown → `modified`.
    """
    status = _KNOWN_STATUSES.get(raw.strip().lower())
    if status is None:
        return "modified", True
    return status, False


def _is_formatting_only(hunks: list[list[str]]) -> bool:
    """Each change block's removed and added lines are equal, in order, ignoring trailing
    whitespace only. Indentation and inner whitespace must match exactly: indentation is
    syntax in Python, and whitespace inside a string literal is a behaviour change.
    """
    blocks = [block for hunk in hunks for block in _change_blocks(hunk)]
    if not blocks:
        return False
    return all(
        [line.rstrip() for line in removed] == [line.rstrip() for line in added]
        for removed, added in blocks
    )


def _is_import_only(path: str, hunks: list[list[str]]) -> bool:
    """Python only: every non-blank changed line is a complete import statement.

    Never for `__init__.py`/`__init__.pyi`: a package's imports there are its public API,
    so adding or removing one is an API change, not a mechanical one.
    """
    normalized = normalize_path(path).lower()
    if not normalized.endswith(_PYTHON_SUFFIXES):
        return False
    if normalized.rsplit("/", 1)[-1] in ("__init__.py", "__init__.pyi"):
        return False
    changed = [
        line[1:]
        for hunk in hunks
        for line in hunk
        if line.startswith(("+", "-")) and line[1:].strip()
    ]
    return bool(changed) and all(_IMPORT_LINE.match(line) for line in changed)


def filter_reason(path: str, status: FileStatus, diff: str) -> str | None:
    """Return why a file is mechanical (PLAN.md §3.1, rows in order), or None.

    An added or deleted file is mechanical only by its path (lockfile, generated,
    vendored) or as a binary file. Adding or removing a module is a structural change
    even if it holds only imports or nothing at all (e.g. an empty `__init__.py`).
    """
    if _matches_path(path, config.NOISE_NAME_GLOBS, config.NOISE_DIR_PATTERNS):
        return _NOISE_REASON
    is_binary = any(marker.search(diff) for marker in _BINARY_MARKERS)
    if status in ("added", "deleted"):
        return "binary or empty" if is_binary else None
    hunks = _hunks(diff)
    if status == "renamed" and hunks is None:
        return "pure rename"
    if not diff.strip() or is_binary:
        return "binary or empty"
    if hunks is None:
        return None  # non-empty text without hunks: malformed, decided in classify_file
    if _is_formatting_only(hunks):
        return "formatting only"
    if _is_import_only(path, hunks):
        return "import only"
    return None


def categorize(path: str, filter_reason: str | None) -> FileCategory:
    """mechanical → test → docs → config → source; the first match wins (PLAN.md §3.1).

    Exception: `CONFIG_BEFORE_DOCS_NAME_GLOBS` (e.g. `requirements.txt`) are config and are
    checked before docs, because the docs `*.txt` rule would otherwise catch them.
    """
    if filter_reason is not None:
        return "mechanical"
    if _matches_path(path, config.TEST_NAME_GLOBS, config.TEST_DIR_PATTERNS):
        return "test"
    if _matches_name(path, config.CONFIG_BEFORE_DOCS_NAME_GLOBS):
        return "config"
    if _matches_path(path, config.DOCS_NAME_GLOBS, config.DOCS_DIR_PATTERNS):
        return "docs"
    if _matches_path(path, config.CONFIG_NAME_GLOBS, config.CONFIG_DIR_PATTERNS):
        return "config"
    return "source"


# ---------------------------------------------------------------------------
# Per-file classification
# ---------------------------------------------------------------------------

_CODE_CATEGORIES: frozenset[FileCategory] = frozenset({"source", "config", "test"})


def _priority(
    *,
    category: FileCategory,
    is_core: bool,
    is_new_module: bool,
    is_sensitive: bool,
    lines_changed: int,
) -> float:
    """PLAN.md §3.2: Σ weight·flag + log10(1 + lines_changed)."""
    return (
        config.PRIORITY_WEIGHT_CORE * is_core
        + config.PRIORITY_WEIGHT_SOURCE * (category == "source")
        + config.PRIORITY_WEIGHT_NEW_MODULE * is_new_module
        + config.PRIORITY_WEIGHT_SENSITIVE * is_sensitive
        + math.log10(1 + lines_changed)
        + config.PRIORITY_WEIGHT_TEST * (category == "test")
        + config.PRIORITY_WEIGHT_DOCS * (category == "docs")
    )


def classify_file(
    file: ChangedFile, core_patterns: Sequence[str]
) -> tuple[FileClassification, list[str]]:
    """Derive every FileClassification field for one file. Returns (classification, warnings).

    Line counts are recomputed from the diff. When the diff has no hunk (malformed,
    binary, empty, pure rename) it can't be counted, so the reported counts are kept.

    A diff with no hunk but reported changes is a missing patch (e.g. a file too large
    for the diff, or a rename whose content changes were left out): it isn't filtered,
    and it's flagged malformed so it routes complex and caps confidence (PLAN.md §9).
    Lockfiles and generated or vendored files stay filtered.
    """
    warnings: list[str] = []
    path = file.path

    status, status_was_unknown = map_status(file.status)
    if status_was_unknown:
        warnings.append(f"{path}: unknown status {file.status!r}, treated as 'modified'")

    reason = filter_reason(path, status, file.diff)
    counts = count_diff_lines(file.diff)
    patch_missing = (
        counts is None
        and file.lines_added + file.lines_removed > 0
        and reason != _NOISE_REASON
        and (reason == "pure rename" or not file.diff.strip())
    )
    if patch_missing:
        reason = None
    category = categorize(path, reason)

    if counts is None:
        lines_added, lines_removed = file.lines_added, file.lines_removed
    else:
        lines_added, lines_removed = counts
        if counts != (file.lines_added, file.lines_removed):
            warnings.append(
                f"{path}: reported +{file.lines_added}/-{file.lines_removed} doesn't match"
                f" the diff (+{lines_added}/-{lines_removed}); using the diff"
            )

    is_malformed = patch_missing or (counts is None and reason is None and bool(file.diff.strip()))
    if patch_missing:
        warnings.append(
            f"{path}: diff has no hunk but +{file.lines_added}/-{file.lines_removed} is reported;"
            " patch missing, the change can't be reviewed"
        )
    elif is_malformed:
        warnings.append(f"{path}: malformed diff (no @@ hunk); treated as raw text")

    is_code = category in _CODE_CATEGORIES
    is_core = _matches_core(path, core_patterns)
    is_sensitive = is_code and any(
        word in path.lower() for word in config.SENSITIVE_PATH_SUBSTRINGS
    )
    is_new_module = status == "added" and category == "source"
    is_unknown_type = (
        category == "source"
        and PurePosixPath(normalize_path(path)).suffix.lower()
        not in config.KNOWN_CODE_EXTENSIONS
    )

    classification = FileClassification(
        path=path,
        status=status,
        status_was_unknown=status_was_unknown,
        lines_added=lines_added,
        lines_removed=lines_removed,
        category=category,
        filter_reason=reason,
        is_core=is_core,
        is_sensitive=is_sensitive,
        is_new_module=is_new_module,
        is_unknown_type=is_unknown_type,
        is_malformed=is_malformed,
        priority=_priority(
            category=category,
            is_core=is_core,
            is_new_module=is_new_module,
            is_sensitive=is_sensitive,
            lines_changed=lines_added + lines_removed,
        ),
    )
    return classification, warnings


# ---------------------------------------------------------------------------
# PR level: duplicate paths
# ---------------------------------------------------------------------------


def merge_duplicate_paths(pr: PRInput) -> tuple[PRInput, list[str]]:
    """Merge entries that repeat a path (PLAN.md §9). Returns (new PR, warnings).

    Paths are compared after normalising (`./src/a.py` is `src/a.py`). The merged entry
    keeps the first entry's path, position and status, joins the diffs with a newline and
    sums the reported line counts. One warning per repeated path.
    """
    merged: dict[str, ChangedFile] = {}
    repeats: dict[str, int] = {}
    for file in pr.changed_files:
        key = normalize_path(file.path)
        first = merged.get(key)
        if first is None:
            merged[key] = file
            continue
        repeats[first.path] = repeats.get(first.path, 1) + 1
        merged[key] = first.model_copy(
            update={
                "diff": first.diff + "\n" + file.diff,
                "lines_added": first.lines_added + file.lines_added,
                "lines_removed": first.lines_removed + file.lines_removed,
            }
        )
    if not repeats:
        return pr, []
    warnings = [
        f"{path}: listed {count} times; entries merged (first status kept)"
        for path, count in repeats.items()
    ]
    return pr.model_copy(update={"changed_files": list(merged.values())}), warnings


# ---------------------------------------------------------------------------
# PR level: secret and injection scans
# ---------------------------------------------------------------------------

# Rule name → regex. A hit stores the rule name only, never the matched text.
_SECRET_RULES: dict[str, re.Pattern[str]] = {
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key_header": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # SECRET_KEY = "…", app.config["SECRET_KEY"] = "…", {"password": "…"}, api_key = "…",
    # AWS_SECRET_ACCESS_KEY="…", csrf_token = "…", API_KEY: str = "…", and camelCase
    # names that end in a keyword: clientSecret, stripeApiKey, dbPassword, accessToken.
    # The keyword must start a word (after a non-letter, or as a capitalised camelCase
    # part) and may only be followed by a key-like suffix, so `tokenizer`, `token_url`,
    # `isSecretValid` and `password_reset_template` don't match. Not os.environ[...]
    # lookups, empty values or values shorter than 8 characters.
    "secret_assignment": re.compile(
        r"(?:(?<![A-Za-z0-9])(?i:api[_-]?key|secret|token|passw(?:or)?d)"
        r"|(?<=[a-z0-9])(?:Api[_-]?Key|Secret|Token|Passw(?:or)?d))"
        r"(?i:[_-]?(?:access[_-]?)?(?:key|token|secret))?"
        r"[\"']?\s*\]?\s*(?::\s*[\w\[\]., |]+?\s*)?[:=]\s*[\"'][^\"'\s]{8,}[\"']"
    ),
    # A hard-coded fallback: os.environ.get("SECRET_KEY", "…") or os.getenv("API_TOKEN", "…").
    "secret_env_default": re.compile(
        r"(?:environ\.get|getenv)\(\s*[\"'][^\"']*(?:key|secret|token|passw(?:or)?d)[^\"']*[\"']"
        r"\s*,\s*[\"'][^\"'\s]{8,}[\"']",
        re.IGNORECASE,
    ),
}

# In the notation of PLAN.md, words in parentheses are optional.
# Small widenings beyond the plan's list: "ignore the previous instructions" (optional
# "the") and "low-risk" with a hyphen.
_INJECTION_PATTERNS: dict[str, str] = {
    "ignore_previous_instructions": (
        r"\b(ignore|disregard)\s+((all|any)\s+)?(the\s+)?(previous|prior|above)\s+instructions\b"
    ),
    "role_override": r"\byou\s+are\s+now\b",
    "system_prompt": r"\bsystem\s+prompt\b",
    "skip_review": r"\bskip\s+(the\s+)?review\b",
    "do_not_review": r"\bdo\s+not\s+review\b",
    "approve_request": r"\bapprove\s+this(\s+(pr|pull\s+request))?\b",
    "mark_safe": r"\bmark\s+(this|it)\s+as\s+(safe|low[\s-]risk)\b",
}
_INJECTION_RULES: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern, re.IGNORECASE) for name, pattern in _INJECTION_PATTERNS.items()
}


def _added_lines(diff: str) -> list[str]:
    """Lines added by a diff. A diff without hunks is raw text: every line counts."""
    hunks = _hunks(diff)
    if hunks is None:
        return _split_lines(diff)
    return [line[1:] for hunk in hunks for line in hunk if line.startswith("+")]


def _rule_hits(
    kind: Literal["secret", "injection"],
    rules: dict[str, re.Pattern[str]],
    file: str | None,
    lines: Sequence[str],
) -> list[ScanHit]:
    """One hit per matching rule for this source (choice: not one per matching line)."""
    return [
        ScanHit(kind=kind, file=file, pattern=name)
        for name, rule in rules.items()
        if any(rule.search(line) for line in lines)
    ]


def scan_pr(pr: PRInput) -> list[ScanHit]:
    """Scan for secrets and prompt-injection phrases (PLAN.md §9).

    Both rule sets run on the title and description (file=None) and on the added lines of
    every file, mechanical files included. Removed lines are never scanned: deleting a
    secret is a fix, not a risk.
    """
    pr_text = _split_lines(pr.pr_title) + _split_lines(pr.pr_description)
    hits = _rule_hits("secret", _SECRET_RULES, None, pr_text)
    hits += _rule_hits("injection", _INJECTION_RULES, None, pr_text)
    for file in pr.changed_files:
        added = _added_lines(file.diff)
        hits += _rule_hits("secret", _SECRET_RULES, file.path, added)
        hits += _rule_hits("injection", _INJECTION_RULES, file.path, added)
    return hits


def secret_in_diff(diff: str) -> bool:
    """Whether any secret rule matches any line of the diff's hunks: added, removed and
    context lines (without their marker) and the `@@` headers. `scan_pr` checks added lines
    only, since deleting a secret is a fix; this wider check guards text that leaves the
    machine (the Jev payload sends whole hunks, Phase 6.10 CR1)."""
    lines = diff_body_lines(diff) + [header for _, header in hunk_headers(diff)]
    return any(rule.search(line) for rule in _SECRET_RULES.values() for line in lines)


# ---------------------------------------------------------------------------
# PR level: stats and the review plan
# ---------------------------------------------------------------------------


def kept_files(classifications: Mapping[str, FileClassification]) -> list[FileClassification]:
    """Non-filtered files, in input order: the ones with something to review."""
    return [c for c in classifications.values() if c.filter_reason is None]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def cut_text(text: str, limit: int) -> str:
    """`text`, or its first `limit` characters and how many more were cut. The one cut
    marker for bounded output (the agent's tools, the Jev payload)."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more characters]"


def is_code_file(classification: FileClassification) -> bool:
    """A non-filtered source, config or test file: what `code_files` and `code_lines_changed` count."""
    return (
        classification.filter_reason is None and classification.category in _CODE_CATEGORIES
    )


def compute_stats(
    pr: PRInput,
    classifications: Mapping[str, FileClassification],
    scan_hits: list[ScanHit],
) -> PRStats:
    """PR-level aggregates. Line counts are the recomputed ones from classify_file."""
    files = list(classifications.values())
    kept_paths = {c.path for c in kept_files(classifications)}
    kept_diff_chars = sum(len(f.diff) for f in pr.changed_files if f.path in kept_paths)
    text_chars = len(pr.pr_title) + len(pr.pr_description) + kept_diff_chars
    return PRStats(
        files_total=len(files),
        code_files=sum(is_code_file(c) for c in files),
        source_files=sum(c.filter_reason is None and c.category == "source" for c in files),
        filtered_files=sum(c.filter_reason is not None for c in files),
        lines_added=sum(c.lines_added for c in files),
        lines_removed=sum(c.lines_removed for c in files),
        code_lines_changed=sum(c.lines_added + c.lines_removed for c in files if is_code_file(c)),
        estimated_tokens=math.ceil(text_chars / config.CHARS_PER_TOKEN),
        scan_hits=list(scan_hits),
    )


def ranked_paths(classifications: Mapping[str, FileClassification]) -> list[str]:
    """Non-filtered files, highest priority first, then by path: the review order.

    The single definition, used by the review plan (`ranked_files`) and the agent's tools.
    """
    return [c.path for c in sorted(kept_files(classifications), key=lambda c: (-c.priority, c.path))]


def review_order(classifications: Mapping[str, FileClassification]) -> list[FileClassification]:
    """Every file: the non-filtered ones in review order (`ranked_paths`), then the
    mechanical ones in input order. Used by the agent's file list and the Jev payload."""
    ranked = [classifications[path] for path in ranked_paths(classifications)]
    return ranked + [c for c in classifications.values() if c.filter_reason is not None]


def build_review_plan(
    classifications: Mapping[str, FileClassification], stats: PRStats
) -> ReviewPlan:
    """Ranked files, must-review files and budgets (PLAN.md §3.2).

    must_review: non-filtered core, sensitive and new-module files, the top-N ranked
    files, and every file with a scan hit (filtered ones too, appended after the ranked
    ones). A filtered file is only included through a scan hit (choice: nothing to review).
    """
    kept = kept_files(classifications)
    ranked = ranked_paths(classifications)
    top = set(ranked[: config.MUST_REVIEW_TOP_N])
    flagged = {c.path for c in kept if c.is_core or c.is_sensitive or c.is_new_module}
    hit_files = {hit.file for hit in stats.scan_hits if hit.file is not None}

    must_review = [p for p in ranked if p in top or p in flagged or p in hit_files]
    must_review += [  # filtered files with a scan hit, after the ranked ones
        p for p, c in classifications.items() if p in hit_files and c.filter_reason is not None
    ]

    tool_budget = config.TOOL_BUDGET_BASE + config.TOOL_BUDGET_PER_CODE_FILE * stats.code_files
    return ReviewPlan(
        ranked_files=ranked,
        must_review=must_review,
        architecture_diff_budget_tokens=config.ARCH_DIFF_BUDGET_TOKENS,
        max_tool_calls=int(clamp(tool_budget, config.TOOL_BUDGET_MIN, config.TOOL_BUDGET_MAX)),
    )


# ---------------------------------------------------------------------------
# Inputs for the LLM nodes (§3.4, §8): API surface and diff selection
# ---------------------------------------------------------------------------

# Top-level definitions only: at column 0, so methods and nested functions are skipped.
# Public names only: `_private` helpers aren't API (the same rule as for assignments).
_TOP_LEVEL_DEF = re.compile(r"^(async\s+def|def|class)\s+([A-Za-z]\w*)")
# A public module-level name being assigned (`NAME = …`, `NAME: T = …`), not compared
# (`==`) or augmented (`+=`). An annotation must look like a type: dotted names, optional
# [...] subscripts, joined by `|` (`int`, `dict[str, list[int]]`, `int | None`), so prose
# in a docstring ("Note: see this = that", "Note: see https://x?a=1") isn't a name.
# `__all__` counts: it defines the public API. Python keywords (`else: y = 2`) are skipped.
# Known limitation (rare, accepted): a keyword argument written at column 0 inside a call
# (`a=1,`) is indistinguishable from a valid tuple assignment and is reported as a name.
_TYPE = r"[\w.]+(?:\[[\w\[\]., |]*\])?"
_PUBLIC_NAME = re.compile(
    rf"^(__all__|[A-Za-z]\w*)\s*(?::\s*{_TYPE}(?:\s*\|\s*{_TYPE})*\s*)?=(?!=)"
)


def _surface_item(line: str) -> tuple[str, str] | None:
    """(kind, name) for a public top-level def/class or a public assignment, else None."""
    if match := _TOP_LEVEL_DEF.match(line):
        return " ".join(match.group(1).split()), match.group(2)
    if (match := _PUBLIC_NAME.match(line)) and not keyword.iskeyword(match.group(1)):
        return "name", match.group(1)
    return None


def api_surface(pr: PRInput, plan: ReviewPlan) -> list[str]:
    """Top-level API changes in the non-filtered Python files, in ranked_files order (§3.4).

    One entry per name, e.g. "src/a.py: added class RateLimiter". "changed" means the name
    was both removed and added with a different definition line (a new signature, or
    `def` → `async def`); identical lines on both sides are a move, not an API change, and
    are left out. Only Python is parsed; other files contribute nothing (the prompt says
    so). Line-based, so a `def` at column 0 inside a multi-line string is a rare accepted
    false positive.
    """
    diffs = {file.path: file.diff for file in pr.changed_files}
    entries: list[str] = []
    for path in plan.ranked_files:
        if not path.lower().endswith(_PYTHON_SUFFIXES) or (hunks := _hunks(diffs.get(path, ""))) is None:
            continue
        # name → {"+": {definition line: kind}, "-": {...}}, in first-seen order
        sides: dict[str, dict[str, dict[str, str]]] = {}
        for hunk in hunks:
            for line in hunk:
                sign, text = line[:1], line[1:].rstrip()
                if sign in ("+", "-") and (item := _surface_item(text)) is not None:
                    kind, name = item
                    sides.setdefault(name, {"+": {}, "-": {}})[sign][text] = kind
        for name, seen in sides.items():
            added, removed = seen["+"], seen["-"]
            if added and removed:
                if set(added) == set(removed):
                    continue  # moved, unchanged
                change = "changed"
            elif added:
                change = "added"
            else:
                change = "removed"
            kind = next(iter((added or removed).values()))  # the new kind when changed
            entries.append(f"{path}: {change} {kind} {name}")
    return entries


def select_diffs(pr: PRInput, plan: ReviewPlan, budget_tokens: int) -> tuple[list[str], list[str]]:
    """(included, cut): whole diffs of ranked_files, highest priority first, within the budget.

    A file is never cut mid-diff (a partial diff reads as a different change). A file that
    doesn't fit is skipped, and smaller lower-priority files may still use the room left.
    Sizes are estimated with CHARS_PER_TOKEN. The cut files make the review "truncated".
    """
    diffs = {file.path: file.diff for file in pr.changed_files}
    included: list[str] = []
    cut: list[str] = []
    used = 0
    for path in plan.ranked_files:
        size = math.ceil(len(diffs.get(path, "")) / config.CHARS_PER_TOKEN)
        if used + size <= budget_tokens:
            included.append(path)
            used += size
        else:
            cut.append(path)
    return included, cut


# ---------------------------------------------------------------------------
# Entry point for parse_pr
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Preprocessed:
    """Everything parse_pr derives from the input. `notes` go to reasoning_trace."""

    pr: PRInput  # after merging duplicate paths
    classifications: dict[str, FileClassification]  # by path, in input order
    stats: PRStats
    plan: ReviewPlan
    notes: tuple[str, ...]


def _totals_notes(pr: PRInput, stats: PRStats) -> list[str]:
    """Warn when the reported top-level totals don't match the recomputed ones.

    `pr` is the input before duplicate paths are merged, so `files_changed` may count
    either the entries or the merged files; a mismatch is reported only if it matches
    neither (duplicates already get their own warning).
    """
    notes = []
    for field, reported, accepted in (
        ("files_changed", pr.files_changed, {stats.files_total, len(pr.changed_files)}),
        ("total_additions", pr.total_additions, {stats.lines_added}),
        ("total_deletions", pr.total_deletions, {stats.lines_removed}),
    ):
        if reported is not None and reported not in accepted:
            actual = min(accepted)
            notes.append(f"reported {field}={reported} doesn't match the diffs ({actual}); using the diffs")
    return notes


def preprocess(pr: PRInput, core_patterns: Sequence[str]) -> Preprocessed:
    """Run every deterministic step of parse_pr: merge, classify, scan, stats, plan."""
    notes: list[str] = []
    for field in ("pr_description", "author"):
        if not getattr(pr, field).strip():  # missing, null or empty (choice: all the same)
            notes.append(f"{field} is missing or empty")

    merged, merge_warnings = merge_duplicate_paths(pr)
    notes += merge_warnings

    classifications: dict[str, FileClassification] = {}
    for file in merged.changed_files:
        classification, file_warnings = classify_file(file, core_patterns)
        classifications[file.path] = classification
        notes += file_warnings

    scan_hits = scan_pr(merged)
    stats = compute_stats(merged, classifications, scan_hits)
    notes += _totals_notes(pr, stats)
    plan = build_review_plan(classifications, stats)
    return Preprocessed(
        pr=merged,
        classifications=classifications,
        stats=stats,
        plan=plan,
        notes=tuple(notes),
    )
