"""Agent worker: the queue loop. What happens to a message lives in the stages (agent/stages/).

Failures split two ways: a transient error (network, rate limit, provider 5xx) raises, so the queue
retries the message; anything else is final and ends the trace in the 'error' state, so a
deterministic failure does not cost five more paid calls.
"""

import logging
import os
from pathlib import Path

from chatbot.agent.pipeline import AgentConfig, Context, run_pipeline
from chatbot.agent.stages import default_stages
from chatbot.channels import Channels, load_channels
from chatbot.config import CONFIG_DIR
from chatbot.interfaces.llm import LlmClient
from chatbot.interfaces.llm_config import from_models
from chatbot.interfaces.llm_manager import LLMManager
from chatbot.models import Event
from chatbot.policy import Policy, load_policy
from chatbot.schedule.client import Calendar
from chatbot.tracing.store import TraceStore

log = logging.getLogger("agent")

DEFAULT_MODEL = "claude-haiku-5-5"  # only for bare-client callers; the agent reads config/llm.toml
INTENT_MAX_TOKENS = 1024
TIER1_MAX_TOKENS = 2048
INTENT_SKIP_CONFIDENCE = float(os.environ.get("INTENT_SKIP_CONFIDENCE", "0.8"))


def handle(event: Event, store: TraceStore, llm: LLMManager | LlmClient, model: str = DEFAULT_MODEL,
           intent_model: str | None = None, intent_skip_confidence: float = INTENT_SKIP_CONFIDENCE,
           calendar: Calendar | None = None, policy: Policy | None = None,
           channels: Channels | None = None) -> None:
    """Run one event through the stages. Raises only for a transient failure, so the queue retries it.

    `llm` is the LLMManager, which reads each component's model from config/llm.toml. A bare client is
    accepted for tests and simple callers: it is wrapped in a manager with the given models, so every call
    still goes through the manager. Without a calendar the calendar stages are skipped. The policy and
    channel routes are read fresh for each message.
    """
    manager = llm if isinstance(llm, LLMManager) else _manager_for_client(llm, store, model, intent_model)
    config = AgentConfig(intent_skip_confidence=intent_skip_confidence)
    ctx = Context(event, store, manager.bind(event.trace_id), config,
                  calendar=calendar, policy=policy or load_policy(), channels=channels or load_channels())
    run_pipeline(default_stages(), ctx)


def _manager_for_client(client: LlmClient, store: TraceStore, model: str, intent_model: str | None) -> LLMManager:
    config = from_models(model, intent_model, INTENT_MAX_TOKENS, TIER1_MAX_TOKENS, effort="low")
    return LLMManager({"anthropic": client}, store, config)


def run_forever(consumer, store: TraceStore, llm: LLMManager, calendar: Calendar | None = None) -> None:
    while True:
        for receipt, event in consumer.receive():
            try:
                handle(event, store, llm, calendar=calendar)
            except Exception:
                # Leave the message on the queue: it reappears after the visibility timeout,
                # and moves to the dead-letter queue after repeated failures.
                log.exception("trace=%s failed", event.trace_id)
                continue
            consumer.delete(receipt)
            log.info("trace=%s finished", event.trace_id)


def make_llm(store: TraceStore) -> LLMManager:
    """The one LLM manager for the agent. Providers come from the environment; models from config/llm.toml.

    Without ANTHROPIC_API_KEY the Anthropic provider is a stub that treats every message as not scheduling.
    A bad config/llm.toml stops startup.
    """
    from chatbot.interfaces.llm import AnthropicClient, StubLlm
    from chatbot.interfaces.openrouter import OpenRouterClient

    providers: dict[str, LlmClient] = {}
    if os.environ.get("ANTHROPIC_API_KEY"):
        providers["anthropic"] = AnthropicClient()
    else:
        log.warning("ANTHROPIC_API_KEY not set: using the stub LLM (every message is treated as not scheduling)")
        providers["anthropic"] = StubLlm()
    if os.environ.get("OPENROUTER_API_KEY"):
        providers["openrouter"] = OpenRouterClient(os.environ["OPENROUTER_API_KEY"])
    path = Path(os.environ.get("LLM_CONFIG_PATH") or CONFIG_DIR / "llm.toml")
    return LLMManager(providers, store, path)


def make_calendar() -> Calendar | None:
    """The calendar service client when CALENDAR_BASE_URL is set (point it at the MIRROR for development)."""
    import json

    from chatbot.schedule.http import HttpCalendar

    url = os.environ.get("CALENDAR_BASE_URL")
    if not url:
        return None
    return HttpCalendar(url, headers=json.loads(os.environ.get("CALENDAR_HEADERS") or "{}"))
