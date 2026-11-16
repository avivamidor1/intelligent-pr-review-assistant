"""Optional Langfuse tracing, for development only (docs/PLAN.md §12).

Tracing is on only when both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set and the
`langfuse` package (the `dev` dependency group) is installed. Otherwise every function here
is a no-op and the app runs exactly as without it. Langfuse is never required.

One graph run gives one nested trace: each node is a span, and every LangChain call made
inside a node (Claude calls, the risk agent's tool calls) nests under it, because LangChain
passes the run's callbacks down to child runs.

The local server lives in `langfuse/` (docker compose); LANGFUSE_BASE_URL points at it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain_core.runnables import RunnableConfig


def langfuse_enabled() -> bool:
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def get_langfuse_handler() -> Any | None:
    """Langfuse's LangChain CallbackHandler, or None when tracing is off."""
    if not langfuse_enabled():
        return None
    try:
        from langfuse.langchain import CallbackHandler
    except ImportError:  # keys set, but the dev group isn't installed
        return None
    return CallbackHandler()


@contextmanager
def traced_run(
    trace_name: str, *, metadata: dict[str, str] | None = None
) -> Iterator[RunnableConfig]:
    """Yield the config to pass to `graph.invoke`, tracing the run when Langfuse is on.

    On exit, pending spans are flushed: a short-lived CLI would otherwise exit before the
    background exporter has sent them.
    """
    handler = get_langfuse_handler()
    if handler is None:
        yield {}
        return

    from langfuse import get_client, propagate_attributes

    try:
        with propagate_attributes(trace_name=trace_name, metadata=metadata):
            yield {"callbacks": [handler], "run_name": trace_name}
    finally:
        get_client().flush()
