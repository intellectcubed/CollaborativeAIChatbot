from datetime import date, datetime

import pytest

from chatbot.schedule.windows import split_span
from chatbot.tracing.store import TraceStore
from chatbot.schedule.mock import MockCalendar
from chatbot.agent.worker import handle
from chatbot.models import Event, RedactedText
from chatbot.policy import Policy
from tests.fakes import FakeLlm, cmd, reply

SAT = date(2026, 11, 7)
MON = date(2026, 11, 9)
DRY = Policy(mode="dry_run")


def test_saturday_afternoon_to_monday_morning_is_four_shifts():
    assert split_span(SAT, "1200", "0600", "2026-11-09") == [
        (SAT, "1200", "1800"),   # Saturday day shift, the rest of it
        (SAT, "1800", "0600"),   # Saturday night shift
        (date(2026, 11, 8), "0600", "1800"),  # Sunday day shift
        (date(2026, 11, 8), "1800", "0600"),  # Sunday night shift, ending Monday morning
    ]


def test_one_shift_is_one_piece_and_keeps_its_hours():
    assert split_span(date(2026, 11, 9), "1800", "0600", "2026-11-10") == [(date(2026, 11, 9), "1800", "0600")]
    assert split_span(date(2026, 11, 9), "1900", "2300") == [(date(2026, 11, 9), "1900", "2300")]


def test_after_midnight_stays_with_its_shift_date():
    assert split_span(MON, "0200", "0300") == [(MON, "0200", "0300")]


def test_a_span_that_starts_at_a_shift_start_is_not_cut_there():
    assert split_span(MON, "1800", "0200") == [(MON, "1800", "0200")]


def test_an_end_before_the_start_is_refused():
    with pytest.raises(ValueError):
        split_span(MON, "1800", "0600", "2026-11-09")  # same day, 0600 is before 1800


def test_the_whole_weekend_change_becomes_four_operations_in_the_plan(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    store.start_trace("t1", "replay", "u", "x")
    cal = MockCalendar()
    cal.seed(SAT, 42, "0600", "1800")  # Saturday day shift
    cal.seed(SAT, 42, "1800", "0600")  # Saturday night shift
    cal.seed(date(2026, 11, 8), 42, "0600", "1800")  # Sunday day shift
    cal.seed(date(2026, 11, 8), 42, "1800", "0600")  # Sunday night shift
    event = Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad="42",
                  sent_at=datetime.fromisoformat("2026-11-07T10:00:00-05:00"), text=RedactedText.mint("msg"), pii_outcome="pass")
    change = cmd("NoCrew", ["42"], "2026-11-07", "1200", "0600")
    change["end_date"] = "2026-11-09"
    handle(event, store, FakeLlm(reply(commands=[change], confidence=0.95)), calendar=cal, policy=DRY)
    steps = {s["name"]: s for s in store.get_trace("t1")["steps"]}
    assert [str(n) for n in steps["validate"]["output"]["notes"]] == [
        "shift split into 4: 2026-11-07 1200-1800 | 2026-11-07 1800-0600 | 2026-11-08 0600-1800 | 2026-11-08 1800-0600"]
    assert len(steps["validate"]["output"]["actions"]) == 4
    actions = steps["validate"]["output"]["actions"]
    assert [(a["date"], a["start"], a["end"], a["status"]) for a in actions] == [
        ("2026-11-07", "1200", "1800", "planned"),
        ("2026-11-07", "1800", "0600", "planned"),
        ("2026-11-08", "0600", "1800", "planned"),
        ("2026-11-08", "1800", "0600", "planned"),
    ]
