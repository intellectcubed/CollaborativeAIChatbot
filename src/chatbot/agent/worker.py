"""Agent worker: the queue loop. What happens to a message lives in the stages (agent/stages/).

Failures split two ways: a transient error (network, rate limit, provider 5xx) raises, so the queue
retries the message; anything else is final and ends the trace in the 'error' state, so a
deterministic failure does not cost five more paid calls.
"""

import logging
import os

from chatbot.agent.pipeline import AgentConfig, Context, run_pipeline
from chatbot.agent.stages import default_stages
from chatbot.interfaces.llm import LlmClient
from chatbot.interfaces.traced_llm import TracedLlm
from chatbot.models import Event
from chatbot.policy import Policy, load_policy
from chatbot.schedule.client import Calendar
from chatbot.tracing.store import TraceStore

log = logging.getLogger("agent")

DEFAULT_MODEL = "claude-haiku-5-5"
INTENT_SKIP_CONFIDENCE = float(os.environ.get("INTENT_SKIP_CONFIDENCE", "0.8"))


def handle(event: Event, store: TraceStore, llm: LlmClient, model: str = DEFAULT_MODEL,
           intent_model: str | None = None, intent_skip_confidence: float = INTENT_SKIP_CONFIDENCE,
           calendar: Calendar | None = None, policy: Policy | None = None) -> None:
    """Run one event through the stages. Raises only for a transient failure, so the queue retries it.

    Without a calendar the calendar stages are skipped. The policy is read fresh for each message.
    """
    config = AgentConfig(model=model, intent_model=intent_model, intent_skip_confidence=intent_skip_confidence)
    ctx = Context(event, store, TracedLlm(llm, store).bind(event.trace_id), config,
                  calendar=calendar, policy=policy or load_policy())
    run_pipeline(default_stages(), ctx)


def run_forever(consumer, store: TraceStore, llm: LlmClient, model: str = DEFAULT_MODEL,
                intent_model: str | None = None, calendar: Calendar | None = None) -> None:
    while True:
        for receipt, event in consumer.receive():
            try:
                handle(event, store, llm, model, intent_model, calendar=calendar)
            except Exception:
                # Leave the message on the queue: it reappears after the visibility timeout,
                # and moves to the dead-letter queue after repeated failures.
                log.exception("trace=%s failed", event.trace_id)
                continue
            consumer.delete(receipt)
            log.info("trace=%s finished", event.trace_id)


def make_llm() -> tuple[LlmClient, str, str | None]:
    """(client, tier 1 model, intent model or None).

    The real client when ANTHROPIC_API_KEY is set, otherwise a stub that says nothing is scheduling.
    INTENT_MODEL=off disables the intent check.
    """
    from chatbot.interfaces.llm import AnthropicClient, StubLlm

    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    if os.environ.get("ANTHROPIC_API_KEY"):
        intent = os.environ.get("INTENT_MODEL", DEFAULT_MODEL)
        return (AnthropicClient(effort=os.environ.get("LLM_EFFORT", "low") or None), model,
                None if intent.lower() in ("", "off") else intent)
    log.warning("ANTHROPIC_API_KEY not set: using the stub LLM (every message is treated as not scheduling)")
    return StubLlm(), "stub", None


def make_calendar() -> Calendar | None:
    """The calendar service client when CALENDAR_BASE_URL is set (point it at the MIRROR for development)."""
    import json

    from chatbot.schedule.http import HttpCalendar

    url = os.environ.get("CALENDAR_BASE_URL")
    if not url:
        return None
    return HttpCalendar(url, headers=json.loads(os.environ.get("CALENDAR_HEADERS") or "{}"))
