"""Provider clients: each one makes a single API call and returns what came back.

Nothing in the system calls these directly. Every model call goes through LLMManager, which picks the
provider from the component's config, enforces budgets, and records cost, tokens, latency and logs.

Only RedactedText may be sent to a model. Every client enforces it.
"""

from dataclasses import dataclass
from typing import Protocol

from chatbot.models import RedactedText

# USD per million tokens (input, output), prompts up to 100K tokens. Unknown models have no price.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
# Prompt caching: a cache write costs 1.25x the input price, a cache read 0.1x.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


class LlmError(Exception):
    """The model call finished but its result is unusable. Retrying will not help."""


class LlmRefusal(LlmError):
    pass


class LlmTruncated(LlmError):
    pass


class ProviderError(ConnectionError):
    """The provider is overloaded, rate limiting or down. Worth retrying later."""


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None
    request_id: str | None = None
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_cost_usd: float | None = None  # when the provider reports the cost itself (OpenRouter does)

    @property
    def cost_usd(self) -> float | None:
        if self.reported_cost_usd is not None:
            return round(self.reported_cost_usd, 6)
        price = PRICES.get(self.model)
        if price is None:
            return None
        p_in, p_out = price
        cost = (self.input_tokens + self.cache_write_tokens * CACHE_WRITE_MULTIPLIER
                + self.cache_read_tokens * CACHE_READ_MULTIPLIER) * p_in
        return round((cost + self.output_tokens * p_out) / 1_000_000, 6)


def is_transient(exc: Exception) -> bool:
    """True for failures worth retrying later (network, rate limit, provider 5xx). The SDK has already retried."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    try:
        import anthropic
    except ImportError:
        return False
    return isinstance(exc, (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError))


class LlmClient(Protocol):
    def complete(self, *, system: str, user: RedactedText, schema: dict, model: str, max_tokens: int,
                 effort: str | None = None, extra: dict | None = None) -> LlmResult: ...


def _require_redacted(user):
    if not isinstance(user, RedactedText):
        raise TypeError("only RedactedText may be sent to a model")


class AnthropicClient:
    """Claude API with structured (JSON schema) output. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, client=None):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client

    def list_models(self) -> list[str]:
        """Every model the account can use, from GET /v1/models (the SDK follows the pages)."""
        return sorted(m.id for m in self.client.models.list())

    def complete(self, *, system, user, schema, model, max_tokens, effort=None, extra=None):
        _require_redacted(user)
        if extra:
            raise LlmError("extra parameters are only supported for OpenRouter models")
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if effort:
            output_config["effort"] = effort
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": str(user)}],
            output_config=output_config,
        )
        if resp.stop_reason == "refusal":
            raise LlmRefusal("model refused")
        if resp.stop_reason == "max_tokens":
            raise LlmTruncated("response hit max_tokens")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return LlmResult(
            text=text,
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            stop_reason=resp.stop_reason,
            request_id=getattr(resp, "_request_id", None),
            cache_read_tokens=getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        )


class StubLlm:
    """Used when no API key is configured: always answers 'not a scheduling message'."""

    def complete(self, *, system, user, schema, model, max_tokens, effort=None, extra=None):
        _require_redacted(user)
        text = '{"is_scheduling": false, "confidence": 0.0, "commands": [], "reasoning": "stub: no API key configured"}'
        return LlmResult(text=text, model="stub", input_tokens=0, output_tokens=0, stop_reason="end_turn")
