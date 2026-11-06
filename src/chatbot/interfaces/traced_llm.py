"""The one place every model call goes through.

TracedLlm wraps any LlmClient with the same interface and records each call as its own trace step
(`llm:<purpose>`): model, prompt version, the exact system and user text, the response, latency,
tokens, cost, request id and stop reason. A failed call is recorded with its exception type.

Stages never talk to a model client directly, so this is also where a budget cap or model routing
would go.
"""

import time

from chatbot.interfaces.llm import LlmClient, LlmResult
from chatbot.models import RedactedText
from chatbot.prompts import Prompt
from chatbot.tracing.store import TraceStore


class BoundLlm:
    """A TracedLlm bound to one message's trace."""

    def __init__(self, inner: LlmClient, store: TraceStore, trace_id: str):
        self.inner, self.store, self.trace_id = inner, store, trace_id

    def complete(self, purpose: str, prompt: Prompt, user: RedactedText, model: str, max_tokens: int) -> LlmResult:
        with self.store.step(self.trace_id, f"llm:{purpose}") as s:
            s.input = {"model": model, "prompt_version": prompt.version, "max_tokens": max_tokens,
                       "system": RedactedText.mint(prompt.system), "user": user}
            t0 = time.monotonic()
            result = self.inner.complete(system=prompt.system, user=user, schema=prompt.schema,
                                         model=model, max_tokens=max_tokens)
            s.output = {"response": RedactedText.mint(result.text), "stop_reason": result.stop_reason,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                        "cost_usd": result.cost_usd, "request_id": result.request_id}
        return result


class TracedLlm:
    def __init__(self, inner: LlmClient, store: TraceStore):
        self.inner, self.store = inner, store

    def bind(self, trace_id: str) -> BoundLlm:
        return BoundLlm(self.inner, self.store, trace_id)
