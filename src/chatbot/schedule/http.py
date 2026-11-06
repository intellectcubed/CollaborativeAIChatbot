"""Client for the calendar HTTP service.

Every call is a GET with an `action` parameter, including the ones that change the calendar. So this
client never retries on its own: a repeated GET would repeat the change. Retrying is the caller's
decision. Errors from the service (`success: false`) raise CalendarError.
"""

from datetime import date

import httpx

from chatbot.schedule.client import Backup, CalendarError, CalendarUnavailable, ChangeResult, RollbackResult
from chatbot.schedule.models import DaySchedule


def _d(shift_date: date) -> str:
    return shift_date.strftime("%Y%m%d")


class HttpCalendar:
    def __init__(self, base_url: str, headers: dict | None = None, timeout: float = 15.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/") + "/"
        self.headers = headers or {}
        self.http = client or httpx.Client(timeout=timeout)

    def _get(self, **params) -> dict:
        try:
            resp = self.http.get(self.base_url, params=params, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TransportError as exc:
            raise CalendarUnavailable(f"{params.get('action')}: {type(exc).__name__}") from exc
        except httpx.HTTPStatusError as exc:
            kind = CalendarUnavailable if exc.response.status_code >= 500 else CalendarError
            raise kind(f"{params.get('action')}: HTTP {exc.response.status_code}") from exc
        except ValueError as exc:
            raise CalendarError(f"{params.get('action')}: unreadable response") from exc
        if not data.get("success"):
            raise CalendarError(f"{params.get('action')}: {data.get('message') or data.get('error') or 'service reported failure'}")
        return data

    def get_day(self, shift_date: date) -> DaySchedule:
        return DaySchedule.from_api(self._get(action="get_schedule_day", date=_d(shift_date)))

    def _change(self, action: str, shift_date: date, start: str, end: str, squad: int, preview: bool) -> ChangeResult:
        data = self._get(action=action, date=_d(shift_date), shift_start=start, shift_end=end,
                         squad=squad, preview="True" if preview else "False")
        return ChangeResult(True, data.get("changeId"), data.get("action", action), data.get("date", _d(shift_date)), data)

    def add_shift(self, shift_date, start, end, squad, preview=False) -> ChangeResult:
        return self._change("addShift", shift_date, start, end, squad, preview)

    def no_crew(self, shift_date, start, end, squad, preview=False) -> ChangeResult:
        return self._change("noCrew", shift_date, start, end, squad, preview)

    def rollback(self, shift_date: date, change_id: str) -> RollbackResult:
        data = self._get(action="rollback", date=_d(shift_date), change_id=change_id)
        return RollbackResult(True, data.get("changeId"), data.get("message", ""), data)

    def list_backups(self, shift_date: date) -> list[Backup]:
        data = self._get(action="list_backups", date=_d(shift_date))
        return [Backup(b["id"], b.get("created_at", ""), b.get("description", ""), b.get("command", ""))
                for b in data.get("backups", [])]
