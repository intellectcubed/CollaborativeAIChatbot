"""Turn the date words a model reports into calendar dates, in code.

The model is good at spotting "Wednesday" in a message and poor at working out which date the upcoming
Wednesday is, so the intent call reports only the words (a DateReference) and this module does the
arithmetic from the message time. A reference that could mean two dates yields both, most likely first.

A shift is named for the day it starts, and a message sent between 0000 and 0600 is about the shift
already in progress, which started the day before. That "shift day" is the base for weekday and
"tonight" references.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from chatbot.config import GROUP_TZ

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
Weekday = Literal["", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SHIFT_CHANGEOVER_HOUR = 6


class DateReference(BaseModel):
    """What a message says about a date, in its own words. Unused fields are "" or 0."""

    weekday: Weekday = ""
    relative: Literal["", "today", "tonight", "tomorrow", "yesterday"] = ""
    qualifier: Literal["", "this", "next"] = ""
    month: int = 0
    day: int = 0

    @property
    def empty(self) -> bool:
        return not (self.weekday or self.relative or (self.month and self.day))

    def label(self) -> str:
        if self.month and self.day:
            return f"{self.month}/{self.day}"
        return " ".join(x for x in (self.qualifier, self.weekday or self.relative) if x)


@dataclass
class Resolution:
    """Every date reference in one message, with the dates each could mean."""

    items: list[tuple[DateReference, list[date]]] = field(default_factory=list)

    @property
    def dates(self) -> list[date]:
        seen: list[date] = []
        for _, ds in self.items:
            seen += [d for d in ds if d not in seen]
        return seen

    def text(self) -> str:
        """One line for the Tier 1 user message."""
        if not self.items:
            return "none mentioned"
        parts = []
        for ref, ds in self.items:
            shown = " or ".join(d.strftime("%A %Y-%m-%d") for d in ds) or "could not be worked out"
            parts.append(f'"{ref.label()}" = {shown}')
        return "; ".join(parts)

    def trace(self) -> list[dict]:
        return [{"reference": r.model_dump(), "dates": [d.isoformat() for d in ds]} for r, ds in self.items]


def shift_day(sent_at: datetime) -> date:
    """The day the shift in progress at `sent_at` started."""
    local = sent_at.astimezone(GROUP_TZ)
    return (local - timedelta(days=1)).date() if local.hour < SHIFT_CHANGEOVER_HOUR else local.date()


def _explicit(month: int, day: int, base: date) -> list[date]:
    """The written date nearest to `base`, in this year, last year or next (so "1/3" in December means January)."""
    options = []
    for year in (base.year - 1, base.year, base.year + 1):
        try:
            options.append(date(year, month, day))
        except ValueError:
            continue  # not a real date, e.g. 9/31
    return [min(options, key=lambda d: abs((d - base).days))] if options else []


def candidates(ref: DateReference, sent_at: datetime) -> list[date]:
    local_today = sent_at.astimezone(GROUP_TZ).date()
    base = shift_day(sent_at)
    if ref.month and ref.day:
        return _explicit(ref.month, ref.day, base)
    if ref.weekday:
        delta = (WEEKDAYS.index(ref.weekday) - base.weekday()) % 7
        coming = base + timedelta(days=delta)
        if ref.qualifier == "this":
            return [coming]
        if ref.qualifier == "next":
            return [coming + timedelta(days=7)] if delta == 0 else [coming, coming + timedelta(days=7)]
        return [coming, coming + timedelta(days=7)] if delta == 0 else [coming]
    if ref.relative == "tonight":
        return [base]
    if ref.relative == "today":
        return [local_today] + ([base] if base != local_today else [])
    if ref.relative == "tomorrow":
        return [local_today + timedelta(days=1)]
    if ref.relative == "yesterday":
        return [local_today - timedelta(days=1)]
    return []


def resolve(references: list[DateReference], sent_at: datetime) -> Resolution:
    return Resolution([(r, candidates(r, sent_at)) for r in references if not r.empty])
