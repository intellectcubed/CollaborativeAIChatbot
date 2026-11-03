"""In-memory calendar for tests and for replay: same interface as HttpCalendar, no network.

It keeps, per shift date, the intervals each squad is scheduled for, rebuilds the service's
shift/segment structure on get_day, snapshots the day before each change, and rolls back to that
snapshot. Territories are not modelled (the real service derives them from fixed rules).
"""

import copy
import uuid
from datetime import date

from chatbot.schedule.client import Backup, CalendarError, ChangeResult, RollbackResult
from chatbot.schedule.models import DaySchedule, Segment, Shift, SquadSlot
from chatbot.schedule.windows import Interval, interval, merge, subtract, to_hhmm


def _label(op: int) -> str:
    h = to_hhmm(op)
    return f"{h[:2]}:{h[2:]}"


class MockCalendar:
    def __init__(self):
        self.days: dict[date, dict[int, list[Interval]]] = {}
        self.snapshots: dict[str, tuple[date, dict[int, list[Interval]]]] = {}
        self.calls: list[tuple] = []

    def seed(self, shift_date: date, squad: int, start: str, end: str) -> None:
        """Schedule a squad without recording a change (test setup)."""
        day = self.days.setdefault(shift_date, {})
        day[squad] = merge([*day.get(squad, []), interval(start, end)])

    # ---- reads

    def get_day(self, shift_date: date) -> DaySchedule:
        self.calls.append(("get_day", shift_date))
        spans = self.days.get(shift_date, {})
        windows = [(1080, 1440), (1440, 1800)]
        if shift_date.weekday() >= 5:
            windows.insert(0, (360, 1080))
        shifts = []
        for ws, we in windows:
            cuts = sorted({ws, we, *(p for ivs in spans.values() for iv in ivs for p in iv if ws < p < we)})
            segments = []
            for a, b in zip(cuts, cuts[1:]):
                squads = [SquadSlot(id=sq, territories=[], active=True)
                          for sq, ivs in sorted(spans.items()) if any(s <= a and b <= e for s, e in ivs)]
                segments.append(Segment(start_time=_label(a), end_time=_label(b), squads=squads))
            shifts.append(Shift(name=f"{_label(ws)} - {_label(we)} Shift", start_time=_label(ws), end_time=_label(we), segments=segments))
        return DaySchedule(day=f"{shift_date:%A %Y-%m-%d}", shifts=shifts)

    def list_backups(self, shift_date: date) -> list[Backup]:
        return [Backup(cid, "", "", "") for cid, (d, _) in self.snapshots.items() if d == shift_date]

    # ---- changes

    def _change(self, action, shift_date, start, end, squad, preview) -> ChangeResult:
        self.calls.append((action, shift_date, start, end, squad, preview))
        iv = interval(start, end)
        if preview:
            return ChangeResult(True, None, action, f"{shift_date:%Y%m%d}", {"preview": True})
        day = self.days.setdefault(shift_date, {})
        change_id = str(uuid.uuid4())
        self.snapshots[change_id] = (shift_date, copy.deepcopy(day))
        current = day.get(squad, [])
        day[squad] = merge([*current, iv]) if action == "addShift" else subtract(current, iv)
        if not day[squad]:
            del day[squad]
        return ChangeResult(True, change_id, action, f"{shift_date:%Y%m%d}")

    def add_shift(self, shift_date, start, end, squad, preview=False) -> ChangeResult:
        return self._change("addShift", shift_date, start, end, squad, preview)

    def no_crew(self, shift_date, start, end, squad, preview=False) -> ChangeResult:
        return self._change("noCrew", shift_date, start, end, squad, preview)

    def rollback(self, shift_date: date, change_id: str) -> RollbackResult:
        self.calls.append(("rollback", shift_date, change_id))
        snap = self.snapshots.pop(change_id, None)
        if snap is None or snap[0] != shift_date:
            raise CalendarError(f"rollback: unknown change {change_id}")
        self.days[shift_date] = snap[1]
        return RollbackResult(True, change_id, f"Rolled back to snapshot {change_id}")
