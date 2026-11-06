"""What the pipeline needs from the calendar service, independent of how it is reached."""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from chatbot.schedule.models import DaySchedule


class CalendarError(Exception):
    """The calendar service refused or failed a request."""


class CalendarUnavailable(CalendarError):
    """The service could not be reached or had a server error. Worth retrying later."""


@dataclass(frozen=True)
class ChangeResult:
    success: bool
    change_id: str | None  # needed to roll the change back; None for a preview
    action: str
    date: str  # yyyymmdd
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class RollbackResult:
    success: bool
    change_id: str | None
    message: str
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Backup:
    id: str
    created_at: str
    description: str
    command: str


class Calendar(Protocol):
    """`shift_date` is always the date the shift starts. Times are 'HHMM'; an end before the start is the next morning."""

    def get_day(self, shift_date: date) -> DaySchedule: ...

    def add_shift(self, shift_date: date, start: str, end: str, squad: int, preview: bool = False) -> ChangeResult: ...

    def no_crew(self, shift_date: date, start: str, end: str, squad: int, preview: bool = False) -> ChangeResult: ...

    def rollback(self, shift_date: date, change_id: str) -> RollbackResult: ...

    def list_backups(self, shift_date: date) -> list[Backup]: ...
