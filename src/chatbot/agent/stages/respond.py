import logging

from chatbot.agent.pipeline import Stage
from chatbot.channels import OutboundMessage
from chatbot.models import RedactedText

log = logging.getLogger("agent")

ASK = "Ask sender: "
CONFIRM = "Ask sender to confirm before changing the calendar: "


class Respond(Stage):
    """Say something back to the sender, through the channel their message came from.

    Questions (clarify, confirm) and refusals (blocked) are always sent. Results (applied, previewed or
    no change) are sent only when the policy says announce_results. Flags for the admin are recorded but
    not posted. Every reply is written to the outbox before delivery, so a redelivered message sends
    nothing twice, and a failed send leaves the trace outcome alone (the calendar may already be changed).
    """

    name = "respond"
    fail_open = True

    def applies(self, ctx):
        return ctx.channels is not None and bool(self._messages(ctx))

    def run(self, ctx, step):
        channel = ctx.channels.for_source(ctx.event.source)
        out = []
        for seq, msg in enumerate(self._messages(ctx), start=1):
            row = {"seq": seq, "kind": msg.kind, "text": msg.text}
            if not ctx.store.outbox_begin(msg.trace_id, seq, msg.kind, msg.to_member, msg.text):
                out.append({**row, "channel": channel.name, "status": "already_sent"})
                continue
            if msg.kind == "flag":
                result_channel, status, error = "-", "skipped", ""  # admin destination is not set up yet
            else:
                try:
                    result = channel.send(msg)
                    result_channel, status, error = channel.name, result.status, result.error
                except Exception as exc:
                    log.warning("trace=%s send via %s failed: %s", msg.trace_id, channel.name, type(exc).__name__)
                    result_channel, status, error = channel.name, "failed", type(exc).__name__
            ctx.store.outbox_finish(msg.trace_id, seq, result_channel, status, error)
            out.append({**row, "channel": result_channel, "status": status, "error": error})
        step.output = {"announce_results": ctx.policy.announce_results, "messages": out}

    @staticmethod
    def _messages(ctx) -> list[OutboundMessage]:
        e = ctx.event

        def msg(kind, text):
            return OutboundMessage(e.trace_id, e.group_id, e.sender_id, e.member, kind, RedactedText.mint(text))

        found = []
        if ctx.plan:
            for a in ctx.plan:
                if a.status == "clarify":
                    found.append(msg("clarify", a.line.removeprefix(ASK)))
                elif a.status == "held":
                    found.append(msg("confirm", "Can you confirm? " + a.line.removeprefix(CONFIRM)))
                elif a.status == "blocked":
                    found.append(msg("blocked", a.line))
                elif a.status == "flagged":
                    found.append(msg("flag", a.line))
                elif a.status in ("applied", "previewed", "noop") and ctx.policy.announce_results:
                    found.append(msg("result", a.line))
        elif ctx.interpretation is not None and ctx.interpretation.is_scheduling:
            # No calendar configured: the interpreter's own questions still go back to the sender.
            found = [msg("clarify", c.question) for c in ctx.interpretation.commands if c.action == "Clarify"]
        return found
