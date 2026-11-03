import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from chatbot.schedule.client import CalendarError
from chatbot.schedule.http import HttpCalendar
from chatbot.schedule.mock import MockCalendar

MONDAY = date(2026, 11, 2)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "get_schedule_day.json").read_text())


def http_calendar(handler):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    return HttpCalendar("http://cal.test/", client=httpx.Client(transport=httpx.MockTransport(wrapped))), seen


def test_get_day_sends_the_documented_request_and_parses_the_response():
    cal, seen = http_calendar(lambda r: httpx.Response(200, json=FIXTURE))
    day = cal.get_day(MONDAY)
    assert dict(seen[0].url.params) == {"action": "get_schedule_day", "date": "20261102"}
    assert seen[0].url.path == "/" and day.coverage()[42] == [(1080, 1800)]


@pytest.mark.parametrize("method,action", [("add_shift", "addShift"), ("no_crew", "noCrew")])
def test_changes_send_the_documented_parameters(method, action):
    cal, seen = http_calendar(lambda r: httpx.Response(200, json={"success": True, "changeId": "c1", "action": action, "date": "20261102"}))
    result = getattr(cal, method)(MONDAY, "2300", "0000", 43)
    assert dict(seen[0].url.params) == {"action": action, "date": "20261102", "shift_start": "2300",
                                        "shift_end": "0000", "squad": "43", "preview": "False"}
    assert result.change_id == "c1" and result.success


def test_preview_flag_is_sent_as_the_service_expects():
    cal, seen = http_calendar(lambda r: httpx.Response(200, json={"success": True, "action": "noCrew", "date": "20261102"}))
    assert cal.no_crew(MONDAY, "2300", "0000", 43, preview=True).change_id is None
    assert seen[0].url.params["preview"] == "True"


def test_rollback_request_and_response():
    body = {"success": True, "message": "Rolled back to snapshot c1", "changeId": "c1", "date": "20261102", "snapshot_removed": True}
    cal, seen = http_calendar(lambda r: httpx.Response(200, json=body))
    result = cal.rollback(MONDAY, "c1")
    assert dict(seen[0].url.params) == {"action": "rollback", "date": "20261102", "change_id": "c1"}
    assert result.success and result.change_id == "c1" and "Rolled back" in result.message


def test_list_backups_parses_the_response():
    body = {"success": True, "action": "list_backups", "date": "20261101", "backups": [
        {"id": "c1", "created_at": "2026-11-02T22:47:47", "description": "addShift - Squad 43 (0600-1800)",
         "command": "action=addShift&date=20261101", "expires_at": "x"}]}
    cal, seen = http_calendar(lambda r: httpx.Response(200, json=body))
    [b] = cal.list_backups(date(2026, 11, 1))
    assert (b.id, b.description) == ("c1", "addShift - Squad 43 (0600-1800)") and seen[0].url.params["action"] == "list_backups"


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"success": False, "message": "bad squad"}),
    httpx.Response(500, text="oops"),
    httpx.Response(200, text="not json"),
])
def test_service_failures_raise_calendar_error_and_are_not_retried(response):
    cal, seen = http_calendar(lambda r: response)
    with pytest.raises(CalendarError):
        cal.no_crew(MONDAY, "2300", "0000", 43)
    assert len(seen) == 1  # a GET that changes the calendar must never be repeated automatically


def test_headers_are_sent_when_configured():
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json=FIXTURE)))
    HttpCalendar("http://cal.test", headers={"X-Api-Key": "k"}, client=client).get_day(MONDAY)
    assert seen[0].headers["x-api-key"] == "k"


# ---- the mock behaves like the service

def seeded():
    cal = MockCalendar()
    cal.seed(MONDAY, 42, "1800", "0600")
    cal.seed(MONDAY, 43, "1800", "0000")
    return cal


def test_mock_reproduces_the_service_shape():
    day = seeded().get_day(MONDAY)
    assert [s.name for s in day.shifts] == ["18:00 - 00:00 Shift", "00:00 - 06:00 Shift"]
    assert day.coverage() == {42: [(1080, 1800)], 43: [(1080, 1440)]}


def test_mock_no_crew_splits_a_shift_and_rollback_restores_it():
    cal = seeded()
    result = cal.no_crew(MONDAY, "2200", "0100", 42)
    assert cal.get_day(MONDAY).coverage()[42] == [(1080, 1320), (1500, 1800)]
    cal.rollback(MONDAY, result.change_id)
    assert cal.get_day(MONDAY).coverage()[42] == [(1080, 1800)]


def test_mock_add_shift_and_idempotence():
    cal = seeded()
    cal.add_shift(MONDAY, "0000", "0200", 43)
    cal.add_shift(MONDAY, "0000", "0200", 43)  # repeating is harmless, as in the service
    assert cal.get_day(MONDAY).coverage()[43] == [(1080, 1560)]
    cal.no_crew(MONDAY, "0300", "0400", 54)  # a squad that is not scheduled: no effect, no error
    assert 54 not in cal.get_day(MONDAY).coverage()


def test_mock_preview_changes_nothing():
    cal = seeded()
    assert cal.no_crew(MONDAY, "2200", "0100", 42, preview=True).change_id is None
    assert cal.get_day(MONDAY).coverage()[42] == [(1080, 1800)]


def test_mock_rollback_of_unknown_change_is_an_error():
    with pytest.raises(CalendarError):
        seeded().rollback(MONDAY, "nope")


def test_mock_weekend_has_a_day_shift():
    cal = MockCalendar()
    saturday = date(2026, 11, 7)
    cal.seed(saturday, 54, "0600", "1800")
    assert [s.start_time for s in cal.get_day(saturday).shifts] == ["06:00", "18:00", "00:00"]
    assert cal.get_day(saturday).coverage() == {54: [(360, 1080)]}
