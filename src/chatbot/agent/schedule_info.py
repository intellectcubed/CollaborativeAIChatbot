"""What the calendar says about the dates a message refers to, and whether the sender's squad is on it.

This is the date-level check that runs before Tier 1. The hours the message names are checked later,
once Tier 1 has read them (agent/stages/validate.py and schedule/verify.py).
"""

from dataclasses import dataclass, field
from datetime import date

from chatbot.schedule.windows import Interval, to_hhmm

NO_SCHEDULE = "not looked up"  # what the Tier 1 message says when no calendar lookup happened


def hours(intervals: list[Interval]) -> str:
    return ", ".join(f"{to_hhmm(s)}-{to_hhmm(e)}" for s, e in intervals)


@dataclass
class DayInfo:
    day: date
    coverage: dict[int, list[Interval]]  # squad -> the intervals it is scheduled for

    def label(self) -> str:
        return self.day.strftime("%A %Y-%m-%d")


@dataclass
class ScheduleInfo:
    squad: int | None  # the sender's squad; None when the sender is not on the roster
    days: list[DayInfo] = field(default_factory=list)

    def scheduled_on(self, info: DayInfo) -> bool:
        return self.squad is not None and self.squad in info.coverage

    @property
    def unscheduled_everywhere(self) -> bool:
        """The sender's squad is on none of the dates. False when the squad or the dates are unknown."""
        return self.squad is not None and bool(self.days) and not any(self.scheduled_on(d) for d in self.days)

    def text(self) -> str:
        """One line per date for the Tier 1 user message."""
        parts = []
        for info in self.days:
            who = "; ".join(f"squad {sq} {hours(iv)}" for sq, iv in sorted(info.coverage.items())) or "nobody scheduled"
            note = ""
            if self.squad is not None:
                note = f" [sender's squad {self.squad}: " + (f"scheduled {hours(info.coverage[self.squad])}" if self.scheduled_on(info) else "NOT scheduled") + "]"
            parts.append(f"{info.label()}: {who}{note}")
        return " | ".join(parts) or NO_SCHEDULE

    def trace(self) -> list[dict]:
        return [{"date": d.day.isoformat(), "squads": {str(sq): hours(iv) for sq, iv in sorted(d.coverage.items())},
                 "sender_squad_scheduled": self.scheduled_on(d)} for d in self.days]

    def question(self) -> str:
        """What to ask the sender when their squad is on none of the dates."""
        when = " or ".join(d.label() for d in self.days)
        on = sorted({sq for d in self.days for sq in d.coverage})
        return (f"It looks like you are asking about a change on {when}, but squad {self.squad} is not on the schedule then"
                + (f" (scheduled: {', '.join(map(str, on))})" if on else "") + ". Did you mean a different day?")
