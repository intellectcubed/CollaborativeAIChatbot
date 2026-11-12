"""Deterministic checks of a requested change against the schedule. No model involved.

A request is checked in two steps:
  1. Is the requested time a real shift time for that shift date? Nights 1800-0600 every day, days
     0600-1800 on Saturday and Sunday only. A 1700 start on a weekday, say, is not: the answer is to ask.
  2. How does it relate to who is actually scheduled?
       remove (no crew): scheduled for all or part of it -> apply (the calendar ignores the unscheduled
                         part, and `partial` says so); not scheduled at all -> nothing to do.
       add:              already scheduled for all of it -> nothing to do; otherwise apply.
"""

from dataclasses import dataclass
from datetime import date

from chatbot.schedule.models import DaySchedule
from chatbot.schedule.windows import Interval, interval, overlap_minutes, valid_windows, within

APPLY, NOOP, CLARIFY = "apply", "noop", "clarify"


@dataclass(frozen=True)
class Verdict:
    status: str  # apply | noop | clarify
    reason: str  # a short code: scheduled, partly_scheduled, not_scheduled, already_scheduled, adds_coverage, ...
    interval: Interval | None = None
    partial: bool = False


def _prepare(shift_date: date, start: str, end: str) -> tuple[Interval | None, Verdict | None]:
    try:
        iv = interval(start, end)
    except ValueError:
        return None, Verdict(CLARIFY, "unreadable_time")
    if not within(valid_windows(shift_date), iv):
        return iv, Verdict(CLARIFY, "outside_shift_window", iv)
    return iv, None


def check_remove(day: DaySchedule, shift_date: date, squad: int, start: str, end: str) -> Verdict:
    iv, problem = _prepare(shift_date, start, end)
    if problem:
        return problem
    covered = overlap_minutes(day.coverage().get(squad, []), iv)
    if covered == 0:
        return Verdict(NOOP, "not_scheduled", iv)
    if covered < iv[1] - iv[0]:
        return Verdict(APPLY, "partly_scheduled", iv, partial=True)
    return Verdict(APPLY, "scheduled", iv)


def check_add(day: DaySchedule, shift_date: date, squad: int, start: str, end: str) -> Verdict:
    iv, problem = _prepare(shift_date, start, end)
    if problem:
        return problem
    covered = overlap_minutes(day.coverage().get(squad, []), iv)
    if covered == iv[1] - iv[0]:
        return Verdict(NOOP, "already_scheduled", iv)
    return Verdict(APPLY, "adds_coverage", iv, partial=covered > 0)
