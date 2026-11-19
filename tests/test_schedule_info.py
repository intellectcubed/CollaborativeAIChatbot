from datetime import date

from chatbot.agent.schedule_info import DayInfo, ScheduleInfo
from chatbot.schedule.models import DaySchedule

MONDAY = date(2026, 11, 2)


def day_with_squad_42_out_until_midnight():
    """Squad 42 was on 1800-0600. It has said it is out 1800-0000, so that part is inactive and the rest is active."""
    return DaySchedule.model_validate({"day": "Monday 2026-11-02", "shifts": [
        {"start_time": "18:00", "end_time": "00:00", "segments": [
            {"start_time": "18:00", "end_time": "00:00", "squads": [{"id": 42, "active": False}]}]},
        {"start_time": "00:00", "end_time": "06:00", "segments": [
            {"start_time": "00:00", "end_time": "06:00", "squads": [{"id": 42, "active": True}]}]},
    ]})


def test_inactive_segments_are_kept_as_no_crew_and_not_as_coverage():
    day = day_with_squad_42_out_until_midnight()
    assert day.coverage() == {42: [(1440, 1800)]}  # 0000-0600 only
    assert day.no_crew() == {42: [(1080, 1440)]}  # 1800-0000 is on the calendar with no crew


def test_a_squad_out_for_the_whole_shift_is_still_on_the_day():
    day = DaySchedule.model_validate({"day": "Monday 2026-11-02", "shifts": [
        {"start_time": "18:00", "end_time": "06:00", "segments": [
            {"start_time": "18:00", "end_time": "06:00", "squads": [{"id": 42, "active": False}]}]}]})
    info = ScheduleInfo(42, [DayInfo(MONDAY, day.coverage(), day.no_crew())])
    assert info.text() == "Monday 2026-11-02: squad 42 No crew 1800-0600 [sender's squad 42: No crew 1800-0600]"
    assert info.trace()[0]["no_crew"] == {"42": "1800-0600"} and not info.trace()[0]["sender_squad_scheduled"]
    assert not info.unscheduled_everywhere  # on the calendar, so the question "is squad 42 on the schedule?" is not asked


def test_the_text_shows_the_partly_out_squad_with_both_parts():
    day = day_with_squad_42_out_until_midnight()
    info = ScheduleInfo(42, [DayInfo(MONDAY, day.coverage(), day.no_crew())])
    assert info.text() == ("Monday 2026-11-02: squad 42 0000-0600, No crew 1800-0000 "
                           "[sender's squad 42: scheduled 0000-0600, No crew 1800-0000]")


def test_a_squad_not_on_the_calendar_is_still_not_scheduled():
    day = day_with_squad_42_out_until_midnight()
    info = ScheduleInfo(54, [DayInfo(MONDAY, day.coverage(), day.no_crew())])
    assert info.text().endswith("[sender's squad 54: NOT scheduled]")
    assert info.unscheduled_everywhere
