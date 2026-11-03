from chatbot.agent.pipeline import Stage


class Receive(Stage):
    name = "agent_received"

    def run(self, ctx, step):
        e = ctx.event
        step.output = {"member": e.member, "squad": e.squad, "sent_at": e.sent_at.isoformat(), "text": e.text}
