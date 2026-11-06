from chatbot.agent import dates, intent, interpret
from chatbot.agent.pipeline import Stage
from chatbot.models import RedactedText
from chatbot.prompts import load_prompt


class IntentCheck(Stage):
    """Ask a small model whether the message looks like a schedule change. Only a confident 'no' stops it.

    Fails open: if the check itself fails, the message goes on to the interpreter.
    """

    name = "intent"
    fail_open = True

    def applies(self, ctx):
        return bool(ctx.config.intent_model)

    def run(self, ctx, step):
        prompt = load_prompt("intent")
        user = interpret.build_user_message(ctx.event, prompt)
        result = ctx.llm.complete("intent", prompt, user, ctx.config.intent_model, ctx.config.intent_max_tokens)
        verdict = intent.parse(result.text)
        skip = intent.should_skip(verdict, ctx.config.intent_skip_confidence)
        ctx.intent = verdict
        ctx.dates = dates.resolve(verdict.date_references, ctx.event.sent_at)
        step.output = {"is_schedule_change": verdict.is_schedule_change, "confidence": verdict.confidence,
                       "reason": RedactedText.mint(verdict.reason), "skip": skip,
                       "dates": ctx.dates.trace(), "dates_text": RedactedText.mint(ctx.dates.text())}
        if skip:
            ctx.outcome = "ignored"
