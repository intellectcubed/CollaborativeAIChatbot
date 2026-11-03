from chatbot.agent.pipeline import Stage


class Gate(Stage):
    """Decide from the interpretation's confidence whether to act, ask the sender first, or flag the admin."""

    name = "gate"

    def applies(self, ctx):
        return any(a.status == "planned" for a in ctx.plan)

    def run(self, ctx, step):
        confidence = ctx.interpretation.confidence
        ctx.decision = ctx.policy.decide(confidence)
        step.output = {"confidence": confidence, "decision": ctx.decision, "mode": ctx.policy.effective_mode,
                       "apply_min": ctx.policy.apply_min_confidence, "ask_min": ctx.policy.ask_min_confidence}
        for a in ctx.plan:
            if a.status != "planned":
                continue
            if ctx.decision == "confirm":
                a.status, a.line = "held", f"Ask sender to confirm before changing the calendar: {a.line}"
            elif ctx.decision == "flag":
                a.status, a.line = "flagged", f"Flag for admin (low confidence {confidence:.2f}): {a.line}"
