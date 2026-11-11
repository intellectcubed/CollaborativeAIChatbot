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
    no_crew: dict[int, list[Interval]] = field(default_factory=dict)  # squad -> intervals it is on but has no crew for

    def label(self) -> str:
        return self.day.strftime("%A %Y-%m-%d")

    def squads(self) -> list[int]:
        return sorted(set(self.coverage) | set(self.no_crew))

    def squad_text(self, squad: int) -> str:
        """e.g. 'squad 42 1800-0600', 'squad 42 No crew 1800-0000', or both for a squad that is partly out."""
        parts = []
        if squad in self.coverage:
            parts.append(hours(self.coverage[squad]))
        if squad in self.no_crew:
            parts.append(f"No crew {hours(self.no_crew[squad])}")
        return f"squad {squad} " + ", ".join(parts)


@dataclass
class ScheduleInfo:
    squad: int | None  # the sender's squad; None when the sender is not on the roster
    days: list[DayInfo] = field(default_factory=list)

    def scheduled_on(self, info: DayInfo) -> bool:
        return self.squad is not None and self.squad in info.coverage

    def on_calendar(self, info: DayInfo) -> bool:
        """Scheduled, or on the calendar with no crew (a squad that is out is still on the day)."""
        return self.squad is not None and (self.squad in info.coverage or self.squad in info.no_crew)

    @property
    def unscheduled_everywhere(self) -> bool:
        """The sender's squad is on none of the dates. False when the squad or the dates are unknown."""
        return self.squad is not None and bool(self.days) and not any(self.on_calendar(d) for d in self.days)

    def text(self) -> str:
        """One line per date for the Tier 1 user message."""
        parts = []
        for info in self.days:
            who = "; ".join(info.squad_text(sq) for sq in info.squads()) or "nobody scheduled"
            note = ""
            if self.squad is not None:
                note = f" [sender's squad {self.squad}: " + self._sender_status(info) + "]"
            parts.append(f"{info.label()}: {who}{note}")
        return " | ".join(parts) or NO_SCHEDULE

    def _sender_status(self, info: DayInfo) -> str:
        if self.scheduled_on(info):
            status = f"scheduled {hours(info.coverage[self.squad])}"
            return status + (f", No crew {hours(info.no_crew[self.squad])}" if self.squad in info.no_crew else "")
        if self.squad in info.no_crew:
            return f"No crew {hours(info.no_crew[self.squad])}"
        return "NOT scheduled"

    def trace(self) -> list[dict]:
        return [{"date": d.day.isoformat(), "squads": {str(sq): hours(iv) for sq, iv in sorted(d.coverage.items())},
                 "no_crew": {str(sq): hours(iv) for sq, iv in sorted(d.no_crew.items())},
                 "sender_squad_scheduled": self.scheduled_on(d)} for d in self.days]

    def question(self) -> str:
        """What to ask the sender when their squad is on none of the dates."""
        when = " or ".join(d.label() for d in self.days)
        on = sorted({sq for d in self.days for sq in d.squads()})
        return (f"It looks like you are asking about a change on {when}, but squad {self.squad} is not on the schedule then"
                + (f" (scheduled: {', '.join(map(str, on))})" if on else "") + ". Did you mean a different day?")
