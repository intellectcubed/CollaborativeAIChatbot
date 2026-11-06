from chatbot.agent.pipeline import Stage
from chatbot.models import RedactedText


class Reply(Stage):
    """Record what the system would say or do.

    With a plan (calendar configured) the lines describe each planned action; otherwise they are the
    interpreter's rendered commands. Final status: flagged if anything needs the admin, done if there is
    anything to report, else ignored.
    """

    name = "reply"

    def run(self, ctx, step):
        lines = [a.line for a in ctx.plan] if ctx.plan else ctx.lines
        step.output = {"lines": [RedactedText.mint(line) for line in lines] or ["no schedule action"]}
        if any(a.status == "flagged" for a in ctx.plan):
            ctx.outcome = "flagged"
        else:
            ctx.outcome = "done" if lines else "ignored"
