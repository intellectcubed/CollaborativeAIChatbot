"""The calendar service's day schedule, as returned by get_schedule_day."""

import json
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from chatbot.schedule.windows import Interval, clock_minutes, interval, merge


class SquadSlot(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    territories: list[int] = Field(default_factory=list)
    active: bool = True


class Segment(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start_time: str
    end_time: str
    squads: list[SquadSlot] = Field(default_factory=list)


class Shift(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""
    start_time: str
    end_time: str
    segments: list[Segment] = Field(default_factory=list)
    tango: int | None = None


class DaySchedule(BaseModel):
    """Everything scheduled for the shifts that START on one date (including their after-midnight tails)."""

    model_config = ConfigDict(extra="ignore")
    day: str = ""
    shifts: list[Shift] = Field(default_factory=list)

    @classmethod
    def from_api(cls, payload: dict) -> "DaySchedule":
        """Parse a get_schedule_day response. Its `day_schedule` field is itself a JSON string."""
        raw = payload["day_schedule"]
        return cls.model_validate(json.loads(raw) if isinstance(raw, str) else raw)

    @property
    def shift_date(self) -> date | None:
        """The date in the `day` label, e.g. 'Monday 2026-11-02'."""
        try:
            return datetime.strptime(self.day.split()[-1], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            return None

    def coverage(self) -> dict[int, list[Interval]]:
        """Active squads and the operational-minute intervals they are scheduled for."""
        spans: dict[int, list[Interval]] = {}
        for shift in self.shifts:
            for seg in shift.segments:
                s, e = interval(seg.start_time, seg.end_time)
                for squad in seg.squads:
                    if squad.active:
                        spans.setdefault(squad.id, []).append((s, e))
        return {squad: merge(v) for squad, v in spans.items()}

    def tango(self) -> int | None:
        return next((s.tango for s in self.shifts if s.tango is not None), None)


__all__ = ["DaySchedule", "Shift", "Segment", "SquadSlot", "clock_minutes"]
