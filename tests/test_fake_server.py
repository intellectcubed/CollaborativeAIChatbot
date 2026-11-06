"""HttpCalendar against the fake service over real HTTP: the two must agree on the protocol."""

import threading
from datetime import date

import pytest

from chatbot.schedule.client import CalendarError
from chatbot.schedule.fake_server import seeded_calendar, serve
from chatbot.schedule.http import HttpCalendar

D = date(2026, 11, 2)


@pytest.fixture
def cal():
    server = serve(0, seeded_calendar())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield HttpCalendar(f"http://127.0.0.1:{server.server_port}")
    server.shutdown()


def test_read_change_preview_and_rollback_over_http(cal):
    assert cal.get_day(D).coverage()[42] == [(1080, 1800)]
    assert cal.no_crew(D, "2200", "0100", 42, preview=True).change_id is None
    assert cal.get_day(D).coverage()[42] == [(1080, 1800)]
    change = cal.no_crew(D, "2200", "0100", 42)
    assert change.change_id and cal.get_day(D).coverage()[42] == [(1080, 1320), (1500, 1800)]
    assert [b.id for b in cal.list_backups(D)] == [change.change_id]
    assert cal.rollback(D, change.change_id).success
    assert cal.get_day(D).coverage()[42] == [(1080, 1800)]


def test_service_errors_surface_as_calendar_errors(cal):
    with pytest.raises(CalendarError):
        cal.rollback(D, "no-such-change")


def test_connection_failure_is_reported_as_unavailable():
    from chatbot.schedule.client import CalendarUnavailable
    with pytest.raises(CalendarUnavailable):
        HttpCalendar("http://127.0.0.1:1", timeout=1).get_day(D)
