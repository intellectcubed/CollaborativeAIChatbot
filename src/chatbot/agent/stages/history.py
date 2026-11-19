from chatbot.agent.history import history_text
from chatbot.agent.pipeline import Stage
from chatbot.models import RedactedText


class ChatHistory(Stage):
    """Before the model calls: read the squad's recent chat (the last 48 hours, from this message's own time).

    The message itself is left out, because it was saved with this trace. Fails open: without history the
    message is still classified, just with less context.
    """

    name = "history"
    fail_open = True

    def run(self, ctx, step):
        ctx.history = history_text(ctx.store, ctx.event.squad, ctx.event.sent_at, ctx.event.trace_id)
        step.output = {"text": RedactedText.mint(ctx.history)}
