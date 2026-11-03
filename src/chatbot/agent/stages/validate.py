from datetime import date

from chatbot.agent.pipeline import Stage
from chatbot.agent.plan import PlannedAction, shift_hint
from chatbot.config import load_allowlist
from chatbot.schedule import verify
from chatbot.schedule.windows import valid_windows


class Validate(Stage):
    """Turn the interpreted commands into planned calendar actions and check each one, in code.

    Needs a calendar. Reads the day's schedule once per date. Never writes. For each squad in a command:
      - asks the sender if the date or hours are missing, or the squad is unknown
      - blocks changes to another squad's shifts (unless policy allows)
      - checks the hours are a real shift time and that the squad is actually scheduled (verify.py)
    """

    name = "validate"

    def applies(self, ctx):
        return ctx.calendar is not None and ctx.interpretation is not None and ctx.interpretation.is_scheduling

    def run(self, ctx, step):
        known = load_allowlist().squads
        days: dict[date, object] = {}
        plan: list[PlannedAction] = []
        for command in ctx.interpretation.commands:
            plan.extend(self._expand(command, ctx, known, days))
        ctx.plan = plan
        step.output = {"actions": [a.trace() for a in plan]}

    def _expand(self, c, ctx, known, days):
        if c.action == "Clarify":
            return [PlannedAction("clarify", status="clarify", reason="model_question", line=f"Ask sender: {c.question}")]
        if c.action == "GetShift":
            return [PlannedAction("other", status="noop", reason="lookup_not_supported",
                                  line=f"Get shift for date {c.date} (not run: lookups are not supported yet)")]
        kind = "remove" if c.action == "NoCrew" else "add"
        actions = []
        for raw in c.squads or [""]:
            a = PlannedAction(kind, shift_date=date.fromisoformat(c.date) if c.date else None, start=c.start, end=c.end)
            if not raw.isdigit() or raw not in known:
                self._set(a, "clarify", "unknown_squad", "Ask sender: Which squad do you mean?")
            elif a.shift_date is None:
                a.squad = int(raw)
                self._set(a, "clarify", "missing_date", f"Ask sender: Which date is that for squad {raw}?")
            elif not (c.start and c.end):
                a.squad = int(raw)
                self._set(a, "clarify", "missing_hours", f"Ask sender: What hours on {a.shift_date} for squad {raw}?")
            else:
                a.squad = int(raw)
                self._check(a, ctx, days)
            actions.append(a)
        return actions

    @staticmethod
    def _set(a, status, reason, line):
        a.status, a.reason, a.line = status, reason, line

    def _check(self, a, ctx, days):
        policy = ctx.policy
        if not policy.allow_cross_squad_edits and str(a.squad) != (ctx.event.squad or ""):
            reason = "unknown_sender" if ctx.event.squad is None else "cross_squad_edit"
            self._set(a, "blocked", reason, f"Not applied: {a.summary()} (the sender's squad is {ctx.event.squad or 'unknown'}; "
                      "only your own squad's shifts can be changed here)")
            return
        if a.shift_date not in days:
            days[a.shift_date] = ctx.calendar.get_day(a.shift_date)
        check = verify.check_remove if a.kind == "remove" else verify.check_add
        v = check(days[a.shift_date], a.shift_date, a.squad, a.start, a.end)
        a.verdict, a.reason = v, v.reason
        if v.status == verify.CLARIFY:
            if v.reason == "outside_shift_window":
                hint = shift_hint(a.shift_date)
                self._set(a, "clarify", v.reason, f"Ask sender: {a.start}-{a.end} is not a shift time on {a.shift_date}; "
                          f"shifts that day run {hint}. What hours do you mean?")
            else:
                self._set(a, "clarify", v.reason, f"Ask sender: I could not read the hours {a.start}-{a.end}. What hours do you mean?")
        elif v.status == verify.NOOP:
            what = "is not scheduled" if v.reason == "not_scheduled" else "is already scheduled"
            self._set(a, "noop", v.reason, f"No change: squad {a.squad} {what} {a.start}-{a.end} on {a.shift_date}")
        else:
            note = " (only part of that time was scheduled)" if v.partial else ""
            self._set(a, "planned", v.reason, f"{a.summary()}{note}")


__all__ = ["Validate", "valid_windows"]
