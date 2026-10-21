"""Agent worker: one Tier 1 model call per message, then the commands it asked for.

Every stage leaves a trace step. Failures split two ways: a transient error (network, rate limit)
raises, so the queue retries the message; an unusable model answer is final and ends the trace in
the 'error' state, so a bad answer does not cost five more paid calls.
"""

import logging
import os

from chatbot.agent import interpret
from chatbot.interfaces.llm import LlmClient, is_transient
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import TraceStore

log = logging.getLogger("agent")

DEFAULT_MODEL = "claude-haiku-5-5"
MAX_TOKENS = 2048


FINAL = {"done", "ignored", "error"}


def handle(event: Event, store: TraceStore, llm: LlmClient, model: str = DEFAULT_MODEL) -> None:
    """Process one event and leave its trace in a final state.

    Raises only for a transient model failure, so the queue retries it. Anything else that goes wrong
    ends the trace in 'error': retrying a deterministic failure would just repeat paid model calls.
    A message whose trace is already final (a redelivery) is skipped.
    """
    tid = event.trace_id
    existing = store.get_trace(tid)
    if existing and existing["status"] in FINAL:
        log.info("trace=%s already %s, skipping redelivery", tid, existing["status"])
        return
    with store.step(tid, "agent_received") as s:
        s.output = {"member": event.member, "squad": event.squad, "sent_at": event.sent_at.isoformat(), "text": event.text}

    prompt = interpret.load_prompt()
    user = interpret.build_user_message(event, prompt)
    try:
        with store.step(tid, "llm_tier1") as s:
            s.input = {"model": model, "prompt_version": prompt.version,
                       "system": RedactedText.mint(prompt.system), "user": user}
            result = llm.complete(system=prompt.system, user=user, schema=prompt.schema,
                                  model=model, max_tokens=MAX_TOKENS)
            s.output = {"response": RedactedText.mint(result.text), "stop_reason": result.stop_reason,
                        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                        "cost_usd": result.cost_usd, "request_id": result.request_id}
    except Exception as exc:
        if is_transient(exc):
            raise
        return _fail(store, tid, exc)

    try:
        interpretation = interpret.parse(result.text)
        lines = [c.render() for c in interpretation.commands] if interpretation.is_scheduling else []
        with store.step(tid, "interpret") as s:
            s.output = {"is_scheduling": interpretation.is_scheduling, "confidence": interpretation.confidence,
                        "reasoning": RedactedText.mint(interpretation.reasoning),
                        "commands": [c.trace_dict() for c in interpretation.commands]}
        with store.step(tid, "reply") as s:
            s.output = {"lines": [RedactedText.mint(line) for line in lines] or ["no schedule action"]}
    except Exception as exc:
        return _fail(store, tid, exc)
    store.set_status(tid, "done" if lines else "ignored")


def _fail(store: TraceStore, tid: str, exc: Exception) -> None:
    log.warning("trace=%s failed: %s", tid, type(exc).__name__)
    store.set_status(tid, "error")


def run_forever(consumer, store: TraceStore, llm: LlmClient, model: str = DEFAULT_MODEL) -> None:
    while True:
        for receipt, event in consumer.receive():
            try:
                handle(event, store, llm, model)
            except Exception:
                # Leave the message on the queue: it reappears after the visibility timeout,
                # and moves to the dead-letter queue after repeated failures.
                log.exception("trace=%s failed", event.trace_id)
                continue
            consumer.delete(receipt)
            log.info("trace=%s finished", event.trace_id)


def make_llm() -> tuple[LlmClient, str]:
    """The real client when ANTHROPIC_API_KEY is set, otherwise a stub that says nothing is scheduling."""
    from chatbot.interfaces.llm import AnthropicClient, StubLlm

    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicClient(effort=os.environ.get("LLM_EFFORT", "low") or None), model
    log.warning("ANTHROPIC_API_KEY not set: using the stub LLM (every message is treated as not scheduling)")
    return StubLlm(), "stub"
