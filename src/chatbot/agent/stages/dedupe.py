import logging

from chatbot.agent.pipeline import FINAL, Stage

log = logging.getLogger("agent")


class DedupeCheck(Stage):
    """A message whose trace is already final was delivered twice: stop without touching the trace."""

    name = "dedupe"
    traced = False

    def run(self, ctx, step):
        existing = ctx.store.get_trace(ctx.event.trace_id)
        if existing and existing["status"] in FINAL:
            log.info("trace=%s already %s, skipping redelivery", ctx.event.trace_id, existing["status"])
            ctx.outcome = "duplicate"
