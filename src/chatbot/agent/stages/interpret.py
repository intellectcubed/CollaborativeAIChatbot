from chatbot.agent import interpret as interpret_mod
from chatbot.agent.pipeline import Stage
from chatbot.agent.schedule_info import NO_SCHEDULE
from chatbot.models import RedactedText
from chatbot.prompts import load_prompt


class Interpret(Stage):
    """Tier 1: one model call that turns the message into commands."""

    name = "interpret"

    def applies(self, ctx):
        return not ctx.short_circuit  # the schedule lookup already has a question for the sender

    def run(self, ctx, step):
        prompt = load_prompt("tier1")
        user = interpret_mod.build_user_message(ctx.event, prompt, ctx.dates.text() if ctx.dates else interpret_mod.NO_DATES,
                                                ctx.schedule.text() if ctx.schedule else NO_SCHEDULE, ctx.history)
        interpretation = interpret_mod.parse(ctx.llm.complete("tier1", prompt, user).text)
        first_confidence = interpretation.confidence
        escalated = self._should_escalate(ctx, interpretation)
        if escalated:
            # One stronger attempt for a message in the low-confidence band. The next message starts again
            # from the configured model, so this is never sticky.
            interpretation = interpret_mod.parse(ctx.llm.complete("tier1", prompt, user, escalate=True).text)
        ctx.interpretation = interpretation
        ctx.lines = [c.render() for c in interpretation.commands] if interpretation.is_scheduling else []
        step.output = {"is_scheduling": interpretation.is_scheduling, "confidence": interpretation.confidence,
                       "reasoning": RedactedText.mint(interpretation.reasoning),
                       "commands": [c.trace_dict() for c in interpretation.commands],
                       "escalated": escalated, "first_confidence": first_confidence}

    @staticmethod
    def _should_escalate(ctx, interpretation) -> bool:
        """Only for a scheduling message whose confidence is in the band the gate would ask the sender about."""
        low = ctx.policy.ask_min_confidence <= interpretation.confidence < ctx.policy.apply_min_confidence
        return interpretation.is_scheduling and low and ctx.llm.has_escalation("tier1")
