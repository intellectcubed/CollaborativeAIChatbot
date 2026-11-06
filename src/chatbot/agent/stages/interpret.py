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
                                                ctx.schedule.text() if ctx.schedule else NO_SCHEDULE)
        result = ctx.llm.complete("tier1", prompt, user, ctx.config.model, ctx.config.max_tokens)
        interpretation = interpret_mod.parse(result.text)
        ctx.interpretation = interpretation
        ctx.lines = [c.render() for c in interpretation.commands] if interpretation.is_scheduling else []
        step.output = {"is_scheduling": interpretation.is_scheduling, "confidence": interpretation.confidence,
                       "reasoning": RedactedText.mint(interpretation.reasoning),
                       "commands": [c.trace_dict() for c in interpretation.commands]}
