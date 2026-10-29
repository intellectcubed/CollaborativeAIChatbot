"""LLM access behind one small interface, so the model or provider can change without touching the pipeline.

Only RedactedText may be sent to a model. Both clients enforce it.
"""

from dataclasses import dataclass
from typing import Protocol

from chatbot.models import RedactedText

# USD per million tokens (input, output), prompts up to 100K tokens. Unknown models cost None.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class LlmError(Exception):
    """The model call finished but its result is unusable. Retrying will not help."""


class LlmRefusal(LlmError):
    pass


class LlmTruncated(LlmError):
    pass


@dataclass(frozen=True)
class LlmResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None
    request_id: str | None = None

    @property
    def cost_usd(self) -> float | None:
        price = PRICES.get(self.model)
        if price is None:
            return None
        return round((self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000, 6)


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
    def complete(self, *, system: str, user: RedactedText, schema: dict, model: str, max_tokens: int) -> LlmResult: ...


def _require_redacted(user):
    if not isinstance(user, RedactedText):
        raise TypeError("only RedactedText may be sent to a model")


class AnthropicClient:
    """Claude API with structured (JSON schema) output. Reads ANTHROPIC_API_KEY from the environment."""

    def __init__(self, client=None, effort: str | None = "low"):
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self.client = client
        self.effort = effort

    def complete(self, *, system, user, schema, model, max_tokens):
        _require_redacted(user)
        output_config = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
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
        )


class StubLlm:
    """Used when no API key is configured: always answers 'not a scheduling message'."""

    def complete(self, *, system, user, schema, model, max_tokens):
        _require_redacted(user)
        text = '{"is_scheduling": false, "confidence": 0.0, "commands": [], "reasoning": "stub: no API key configured"}'
        return LlmResult(text=text, model="stub", input_tokens=0, output_tokens=0, stop_reason="end_turn")
