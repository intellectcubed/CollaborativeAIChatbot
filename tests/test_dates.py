from datetime import date, datetime

import pytest

from chatbot.agent import interpret
from chatbot.agent.dates import DateReference, resolve, shift_day
from chatbot.agent.worker import handle
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import TraceStore
from tests.fakes import NO_CREW_JSON, FakeLlm, intent_reply

MONDAY_2PM = datetime.fromisoformat("2026-11-02T14:10:00-05:00")
WEDNESDAY_2AM = datetime.fromisoformat("2026-11-04T02:00:00-05:00")  # the shift in progress started Tuesday


def dates(ref, sent_at=MONDAY_2PM):
    [(_, found)] = resolve([DateReference(**ref)], sent_at).items
    return found


def test_a_weekday_is_the_coming_one():
    assert dates({"weekday": "wednesday"}) == [date(2026, 11, 4)]
    assert dates({"weekday": "friday", "qualifier": "this"}) == [date(2026, 11, 6)]


def test_the_same_weekday_could_be_today_or_a_week_on():
    assert dates({"weekday": "monday"}) == [date(2026, 11, 2), date(2026, 11, 9)]
    assert dates({"weekday": "monday", "qualifier": "this"}) == [date(2026, 11, 2)]
    assert dates({"weekday": "monday", "qualifier": "next"}) == [date(2026, 11, 9)]


def test_next_weekday_offers_both_readings():
    assert dates({"weekday": "friday", "qualifier": "next"}) == [date(2026, 11, 6), date(2026, 11, 13)]


def test_relative_words():
    assert dates({"relative": "tonight"}) == [date(2026, 11, 2)]
    assert dates({"relative": "tomorrow"}) == [date(2026, 11, 3)]
    assert dates({"relative": "yesterday"}) == [date(2026, 11, 1)]
    assert dates({"relative": "today"}) == [date(2026, 11, 2)]


def test_after_midnight_tonight_is_the_shift_already_in_progress():
    assert shift_day(WEDNESDAY_2AM) == date(2026, 11, 3)
    assert dates({"relative": "tonight"}, WEDNESDAY_2AM) == [date(2026, 11, 3)]
    assert dates({"relative": "today"}, WEDNESDAY_2AM) == [date(2026, 11, 4), date(2026, 11, 3)]
    assert dates({"weekday": "wednesday"}, WEDNESDAY_2AM) == [date(2026, 11, 4)]  # the coming shift
    assert dates({"weekday": "tuesday"}, WEDNESDAY_2AM) == [date(2026, 11, 3), date(2026, 11, 10)]


def test_written_dates_pick_the_nearest_year_and_reject_impossible_ones():
    assert dates({"month": 11, "day": 4}) == [date(2026, 11, 4)]
    assert dates({"month": 1, "day": 3}, datetime.fromisoformat("2026-12-28T12:00:00-05:00")) == [date(2027, 1, 3)]
    assert dates({"month": 9, "day": 31}) == []
    assert dates({"month": 11, "day": 4, "weekday": "friday"}) == [date(2026, 11, 4)]  # the written date wins


def test_empty_references_are_dropped_and_text_reads_well():
    r = resolve([DateReference(), DateReference(weekday="wednesday"), DateReference(weekday="monday")], MONDAY_2PM)
    assert len(r.items) == 2
    assert r.text() == '"wednesday" = Wednesday 2026-11-04; "monday" = Monday 2026-11-02 or Monday 2026-11-09'
    assert resolve([], MONDAY_2PM).text() == "none mentioned"
    assert resolve([DateReference(month=9, day=31)], MONDAY_2PM).text() == '"9/31" = could not be worked out'
    assert r.dates == [date(2026, 11, 4), date(2026, 11, 2), date(2026, 11, 9)]


# ---- through the pipeline

INTENT = "claude-haiku-5-5"


def dates_line(user: str) -> str:
    """The Tier 1 message up to and including its dates line (the calendar line comes after it)."""
    return user.split("\nCalendar for those dates:")[0]


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "t.db")
    s.start_trace("t1", "replay", "u", "x")
    return s


def event():
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad="34", sent_at=MONDAY_2PM,
                 text=RedactedText.mint("34 is challenged for Wednesday"), pii_outcome="pass")


def test_intent_dates_are_worked_out_traced_and_passed_to_tier1(store):
    llm = FakeLlm([intent_reply(True, 0.9, dates=[{"weekday": "wednesday"}]), NO_CREW_JSON])
    handle(event(), store, llm, intent_model=INTENT)
    steps = {s["name"]: s for s in store.get_trace("t1")["steps"]}
    out = steps["intent"]["output"]
    assert out["dates"] == [{"reference": {"weekday": "wednesday", "relative": "", "qualifier": "", "month": 0, "day": 0}, "dates": ["2026-11-04"]}]
    assert out["dates_text"] == '"wednesday" = Wednesday 2026-11-04'
    assert dates_line(llm.calls[1]["user"]).endswith('(worked out by code from its wording): "wednesday" = Wednesday 2026-11-04')


def test_a_message_with_no_date_says_so(store):
    llm = FakeLlm([intent_reply(True, 0.9), NO_CREW_JSON])
    handle(event(), store, llm, intent_model=INTENT)
    assert dates_line(llm.calls[1]["user"]).endswith("wording): none mentioned")


def test_without_the_intent_step_tier1_is_told_dates_were_not_extracted(store):
    llm = FakeLlm(NO_CREW_JSON)
    handle(event(), store, llm)
    assert dates_line(llm.calls[0]["user"]).endswith("wording): not extracted")


def test_an_old_style_intent_answer_without_dates_still_works(store):
    import json
    old = json.dumps({"is_schedule_change": True, "confidence": 0.9, "reason": "r"})
    llm = FakeLlm([old, NO_CREW_JSON])
    handle(event(), store, llm, intent_model=INTENT)
    assert dates_line(llm.calls[1]["user"]).endswith("wording): none mentioned")


def test_the_committed_intent_schema_asks_for_date_references():
    schema = interpret.load_prompt("intent").schema
    assert "date_references" in schema["required"]
    assert set(schema["properties"]["date_references"]["items"]["properties"]) == {"weekday", "relative", "qualifier", "month", "day"}
