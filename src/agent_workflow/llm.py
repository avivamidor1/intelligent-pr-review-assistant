"""The LLM boundary (docs/PLAN.md §5, §9, §10).

Graph nodes call only `structured_call` (and, for the risk agent's recovery call,
`structured_call_messages`) and see only what this module defines: the `Effort` levels,
`Usage`, and the `LLMError` family. The risk agent (create_agent) also gets its chat model
and the provider's middleware from here (`agent_model`, `agent_middleware`, `usage_of`).
Everything provider-specific stays in this module: the Claude model, prompt caching, the
refusal fallback, the provider's usage fields and prices. Adding another provider later
means changing this module, not the graph.

Anthropic (Claude Opus 5.5) is the only provider. Behaviour verified in Phase 4.1:
- structured output is native JSON schema (`output_config.format`); forced tool use is a 400,
- the system prompt carries a cache breakpoint; it caches from 512 tokens,
- with `include_raw=True` a parse failure is returned, not raised, so it's raised here,
- a refusal can't parse either, so it's checked first: refusals fail fast, never retried.

Error policy (§9, Phase 7, D2): `is_degradable` says which failures a node survives as a
degraded stage: our `LLMError` family, and any API error the SDK gave up on after its own
retries. A 401 or 403 is not: a bad key fails every call the same way, so the run stops
(exit 1) instead of writing a review no model wrote. `langchain-anthropic` re-raises the
SDK's errors as its own subclasses of them (checked in 7.1), so the checks use the SDK's
classes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

import anthropic
from langchain.agents.middleware import AgentMiddleware
from langchain_anthropic import ChatAnthropic
from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel

from . import config

Effort = Literal["low", "medium", "high"]
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class LLMError(Exception):
    """An LLM call failed and retrying the same request won't help."""


class LLMRefusal(LLMError):
    """The model declined, even after the provider's fallback. Never retried (§9)."""


class LLMSchemaError(LLMError):
    """The output didn't match the schema. The node's RetryPolicy retries it once (§9)."""


# A bad or unauthorised key: every call would fail the same way, so these are fatal (D2).
_FATAL_PROVIDER_ERRORS = (anthropic.AuthenticationError, anthropic.PermissionDeniedError)


def is_degradable(error: BaseException) -> bool:
    """True if a node that raised `error` (after its retries) should degrade, not fail the
    run: an LLMError, or an API error the SDK gave up on, except a 401/403 (D2). Anything
    else is a bug and must surface."""
    if isinstance(error, _FATAL_PROVIDER_ERRORS):
        return False
    return isinstance(error, (LLMError, anthropic.APIError))


@dataclass(frozen=True)
class Usage:
    """One call's token usage and estimated cost, recorded in the trace (§10.7)."""

    model: str  # the model that answered (a fallback model shows here)
    input_tokens: int  # uncached input
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int  # includes thinking
    cost_usd: float  # an estimate from list prices, never billing

    def __add__(self, other: Usage) -> Usage:
        """Two calls' usage summed: the risk agent's calls go in one trace line (D3)."""
        return Usage(
            model=self.model if self.model == other.model else "mixed",
            input_tokens=self.input_tokens + other.input_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )

    def trace_line(self, label: str) -> str:
        total_in = self.input_tokens + self.cache_read_tokens + self.cache_write_tokens
        return (
            f"LLM {label}: {total_in:,} tokens in ({self.cache_read_tokens:,} cached), "
            f"{self.output_tokens:,} out, ~${self.cost_usd:.3f} ({self.model})."
        )


def structured_call(
    schema: type[SchemaT], *, effort: Effort, system: str, user: str
) -> tuple[SchemaT, Usage]:
    """One LLM call returning a validated `schema` instance and its usage.

    `system` must be static across runs (it's cached); PR content goes in `user`.
    Raises LLMRefusal, LLMSchemaError, or LLMError (e.g. the output hit max_tokens).
    Transient API errors are retried inside the provider's SDK before anything is raised.
    """
    return _structured(schema, effort, _messages(system, user))


def structured_call_messages(
    schema: type[SchemaT], *, effort: Effort, system: str, messages: list[BaseMessage]
) -> tuple[SchemaT, Usage]:
    """Like `structured_call`, over an existing conversation: the risk agent's recovery call
    (§7), which asks for the final answer over the agent's own history. No tools are sent
    (verified in Phase 5.1); the history's thinking blocks are passed back unchanged."""
    return _structured(schema, effort, [_system(system), *messages])


def _structured(schema: type[SchemaT], effort: Effort, messages: list[BaseMessage]) -> tuple[SchemaT, Usage]:
    """The one place a structured call is made: JSON-schema output, raw message kept for usage."""
    model = _claude(effort).with_structured_output(schema, method="json_schema", include_raw=True)
    return _interpret(model.invoke(messages))


# ---------------------------------------------------------------------------
# The risk agent (create_agent) needs the chat model itself and the provider's middleware
# (D1). They are built here, so the rest of the code never imports the provider.
# ---------------------------------------------------------------------------


def agent_model(effort: Effort) -> BaseChatModel:
    """The chat model for the risk agent's loop: the same Claude configuration as
    `structured_call` (effort, max_tokens, SDK retries, the refusal fallback)."""
    return _claude(effort)


def agent_middleware() -> list[AgentMiddleware]:
    """The provider's middleware for the agent: prompt caching of the system prompt, the
    tool definitions and the growing message history (§5.5)."""
    return [AnthropicPromptCachingMiddleware(ttl="5m")]


def usage_of(message: AIMessage) -> Usage:
    """One agent model call's usage, from the API's own usage fields (see `_usage`)."""
    return _usage(message.response_metadata)


# ---------------------------------------------------------------------------
# Claude (the only provider)
# ---------------------------------------------------------------------------


def _model_name() -> str:
    # Read at call time, after .env is loaded (config.MODEL_MAIN explains why).
    return os.getenv("MODEL_MAIN") or config.MODEL_MAIN


def _claude(effort: Effort) -> ChatAnthropic:
    return ChatAnthropic(
        model=_model_name(),
        reasoning_effort=effort,  # thinking stays on (adaptive): it can't be disabled on Opus 5.5
        max_tokens=config.LLM_MAX_TOKENS,
        max_retries=config.LLM_MAX_RETRIES,
        betas=[config.FALLBACK_BETA],
        model_kwargs={"fallbacks": config.FALLBACK_MODE},
    )


def _system(system: str) -> SystemMessage:
    """The static system prompt, with the cache breakpoint."""
    return SystemMessage(content=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}])


def _messages(system: str, user: str) -> list[SystemMessage | HumanMessage]:
    """The static system prompt first, with the cache breakpoint; the PR content after it."""
    return [_system(system), HumanMessage(content=user)]


def _usage(metadata: dict[str, Any]) -> Usage:
    """From the API's own usage fields. LangChain's usage_metadata reports a 5-minute cache
    write as cache_creation 0 (Phase 4.1), so it isn't used."""
    raw = metadata.get("usage") or {}
    input_tokens = int(raw.get("input_tokens") or 0)
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    cache_write = int(raw.get("cache_creation_input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    cost = (
        input_tokens * config.PRICE_INPUT_PER_MTOK
        + cache_read * config.PRICE_CACHE_READ_PER_MTOK
        + cache_write * config.PRICE_CACHE_WRITE_PER_MTOK
        + output_tokens * config.PRICE_OUTPUT_PER_MTOK
    ) / 1_000_000
    return Usage(
        model=str(metadata.get("model") or _model_name()),
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def _interpret(response: dict[str, Any]) -> tuple[Any, Usage]:
    """Map LangChain's `include_raw` result to (parsed, Usage) or one of our errors.

    The order matters: a refusal or a cut-off answer also fails to parse, but retrying
    either would only repeat it, so both are raised as non-retried errors first.
    """
    metadata = response["raw"].response_metadata
    usage = _usage(metadata)
    stop_reason = metadata.get("stop_reason")
    if stop_reason == "refusal":
        category = (metadata.get("stop_details") or {}).get("category")
        raise LLMRefusal(f"{usage.model} declined the request (category: {category})")
    if stop_reason == "max_tokens":
        raise LLMError(f"{usage.model} hit max_tokens ({config.LLM_MAX_TOKENS}) before finishing")
    error = response.get("parsing_error")
    if error is not None or response.get("parsed") is None:
        raise LLMSchemaError(f"output didn't match the schema: {error}") from error
    return response["parsed"], usage
