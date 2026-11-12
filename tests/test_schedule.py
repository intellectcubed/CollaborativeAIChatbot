import json
from datetime import date
from pathlib import Path

import pytest

from chatbot.schedule import verify, windows
from chatbot.schedule.models import DaySchedule

MONDAY, SATURDAY = date(2026, 11, 2), date(2026, 11, 7)
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "get_schedule_day.json").read_text())


@pytest.fixture
def monday():
    return DaySchedule.from_api(FIXTURE)


@pytest.mark.parametrize("start,end,expected", [
    ("1800", "0600", (1080, 1800)),   # a whole night
    ("0600", "1800", (360, 1080)),    # a whole weekend day
    ("2200", "0100", (1320, 1500)),   # crosses midnight
    ("0200", "0300", (1560, 1620)),   # entirely after midnight: still the same shift date
    ("1800", "0000", (1080, 1440)),
    ("0000", "0600", (1440, 1800)),
    ("18:00", "00:00", (1080, 1440)),
])
def test_interval_places_times_on_the_operational_day(start, end, expected):
    assert windows.interval(start, end) == expected


@pytest.mark.parametrize("bad", ["", "18", "2500", "1860", "ab12"])
def test_unreadable_times_are_rejected(bad):
    with pytest.raises(ValueError):
        windows.interval(bad, "0600")


def test_valid_windows_nights_always_days_only_on_weekends():
    assert windows.valid_windows(MONDAY) == [(1080, 1800)]
    assert windows.valid_windows(SATURDAY) == [(360, 1800)]  # day and night touch, so they merge


def test_interval_arithmetic():
    assert windows.merge([(10, 20), (20, 30), (40, 50)]) == [(10, 30), (40, 50)]
    assert windows.subtract([(10, 50)], (20, 30)) == [(10, 20), (30, 50)]
    assert windows.subtract([(10, 50)], (0, 60)) == []
    assert windows.overlap_minutes([(10, 20), (30, 40)], (15, 35)) == 10
    assert windows.to_hhmm(1560) == "0200" and windows.to_hhmm(1080) == "1800"


def test_the_service_response_parses_its_nested_json(monday):
    assert monday.shift_date == MONDAY and monday.tango() == 42
    assert monday.coverage() == {42: [(1080, 1800)], 43: [(1080, 1440)]}  # 42 all night, 43 until midnight


def test_inactive_squads_are_not_scheduled(monday):
    monday.shifts[0].segments[0].squads[1].active = False
    assert 43 not in monday.coverage()


@pytest.mark.parametrize("squad,start,end,status,reason,partial", [
    (42, "2200", "0100", "apply", "scheduled", False),
    (42, "0200", "0300", "apply", "scheduled", False),         # after midnight, still Monday's shift
    (43, "2300", "0100", "apply", "partly_scheduled", True),   # 43 is only on until midnight
    (43, "0200", "0300", "noop", "not_scheduled", False),
    (54, "1800", "0600", "noop", "not_scheduled", False),
    (42, "1700", "1800", "clarify", "outside_shift_window", False),  # no weekday day shift
    (42, "0600", "0700", "clarify", "outside_shift_window", False),
    (42, "1800", "abcd", "clarify", "unreadable_time", False),
])
def test_remove_checks_against_who_is_scheduled(monday, squad, start, end, status, reason, partial):
    v = verify.check_remove(monday, MONDAY, squad, start, end)
    assert (v.status, v.reason, v.partial) == (status, reason, partial)


@pytest.mark.parametrize("squad,start,end,status,reason", [
    (42, "1900", "2000", "noop", "already_scheduled"),
    (43, "0000", "0200", "apply", "adds_coverage"),
    (43, "2200", "0200", "apply", "adds_coverage"),            # partly covered: still adds
    (54, "1800", "0600", "apply", "adds_coverage"),
    (54, "1700", "1800", "clarify", "outside_shift_window"),
])
def test_add_checks_for_existing_coverage(monday, squad, start, end, status, reason):
    v = verify.check_add(monday, MONDAY, squad, start, end)
    assert (v.status, v.reason) == (status, reason)


def test_a_weekend_day_shift_is_a_real_window(monday):
    saturday = DaySchedule(day="Saturday 2026-11-07", shifts=[])
    assert verify.check_add(saturday, SATURDAY, 54, "1700", "1800").status == "apply"
    assert verify.check_remove(saturday, SATURDAY, 54, "1700", "1800").status == "noop"
