from chatbot.agent.pipeline import Stage
from chatbot.agent.plan import PlannedAction
from chatbot.agent.schedule_info import DayInfo, ScheduleInfo
from chatbot.models import RedactedText

MAX_DATES = 4


class ScheduleLookup(Stage):
    """Before Tier 1: fetch the calendar for the dates the message refers to and see whether the sender's squad is on it.

    Tier 1 is shown the schedule. If the policy says ask_if_unscheduled, the intent step read the message as a
    squad giving up coverage (direction "remove"), and the sender's squad is on none of the dates, Tier 1 is skipped
    and the sender is asked which day they meant. A squad adding a shift is often not scheduled, so adds never ask.

    Fails open: if the calendar cannot be read the message goes on without a schedule (a service outage is retried).
    The hours in the message are checked later, once Tier 1 has read them.
    """

    name = "schedule"
    fail_open = True

    def applies(self, ctx):
        return ctx.calendar is not None and ctx.dates is not None and bool(ctx.dates.dates)

    def run(self, ctx, step):
        squad = ctx.event.squad
        days = []
        for d in ctx.dates.dates[:MAX_DATES]:
            if d not in ctx.day_cache:
                ctx.day_cache[d] = ctx.calendar.get_day(d)
            days.append(DayInfo(d, ctx.day_cache[d].coverage()))
        info = ScheduleInfo(int(squad) if squad and squad.isdigit() else None, days)
        ctx.schedule = info
        remove = ctx.intent is not None and ctx.intent.direction == "remove"
        ask = ctx.policy.ask_if_unscheduled and remove and info.unscheduled_everywhere
        step.output = {"sender_squad": info.squad, "direction": ctx.intent.direction if ctx.intent else None,
                       "days": info.trace(), "schedule_text": RedactedText.mint(info.text()),
                       "decision": "ask_sender" if ask else "continue_to_tier1"}
        if ask:
            question = info.question()
            ctx.plan = [PlannedAction("clarify", squad=info.squad, status="clarify", reason="squad_not_scheduled",
                                      line="Ask sender: " + question)]
            ctx.short_circuit = True
