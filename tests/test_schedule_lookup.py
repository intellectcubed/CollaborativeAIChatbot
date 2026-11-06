from datetime import date, datetime

import pytest

from chatbot.agent.worker import handle
from chatbot.channels import Channels
from chatbot.channels.builtin import ReplayChannel
from chatbot.models import Event, RedactedText
from chatbot.policy import Policy
from chatbot.schedule.client import CalendarError, CalendarUnavailable
from chatbot.schedule.mock import MockCalendar
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, cmd, intent_reply, reply

MONDAY_2PM = datetime.fromisoformat("2026-11-02T14:10:00-05:00")
WEDNESDAY = date(2026, 11, 4)
INTENT = "claude-haiku-5-5"
ASK = Policy(ask_if_unscheduled=True)
CHANNELS = Channels({"replay": ReplayChannel()}, {"replay": "replay"})
NO_CREW = reply(commands=[cmd("NoCrew", ["34"], "2026-11-04", "2200", "0100")])
WED = [{"weekday": "wednesday"}]


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "t.db")
    s.start_trace("t1", "replay", "u", "x")
    return s


@pytest.fixture
def cal():
    c = MockCalendar()
    c.seed(WEDNESDAY, 43, "1800", "0600")
    c.seed(WEDNESDAY, 54, "1800", "0000")
    return c


def event(squad="34"):
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad=squad, sent_at=MONDAY_2PM,
                 text=RedactedText.mint("34 is challenged for Wednesday"), pii_outcome="pass")


def run(store, cal, llm, policy=ASK, squad="34"):
    handle(event(squad), store, llm, intent_model=INTENT, calendar=cal, policy=policy, channels=CHANNELS)
    t = store.get_trace("t1")
    return t, {s["name"]: s for s in t["steps"]}


def test_a_scheduled_squad_continues_and_tier1_sees_the_calendar(store, cal):
    cal.seed(WEDNESDAY, 34, "1800", "0000")
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove"), NO_CREW])
    t, st = run(store, cal, llm)
    assert len(llm.calls) == 2 and st["schedule"]["output"]["decision"] == "continue_to_tier1"
    assert st["schedule"]["output"]["days"][0]["sender_squad_scheduled"] is True
    line = "Calendar for those dates: Wednesday 2026-11-04: squad 34 1800-0000; squad 43 1800-0600; squad 54 1800-0000 [sender's squad 34: scheduled 1800-0000]"
    assert llm.calls[1]["user"].endswith(line)


def test_a_removing_squad_that_is_not_scheduled_is_asked_without_calling_tier1(store, cal):
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove")])
    t, st = run(store, cal, llm)
    assert len(llm.calls) == 1 and "llm:tier1" not in st and "interpret" not in st
    assert st["schedule"]["output"]["decision"] == "ask_sender"
    [row] = store.get_outbox("t1")
    assert (row["kind"], row["channel"], row["status"]) == ("clarify", "replay", "sent")
    assert row["text"] == ("It looks like you are asking about a change on Wednesday 2026-11-04, but squad 34 is not on the "
                           "schedule then (scheduled: 43, 54). Did you mean a different day?")
    assert t["status"] == "done"


def test_an_adding_squad_is_never_asked(store, cal):
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="add"), NO_CREW])
    run(store, cal, llm)
    assert len(llm.calls) == 2  # taking extra coverage is normally not scheduled
    assert "[sender's squad 34: NOT scheduled]" in llm.calls[1]["user"]


def test_an_unclear_direction_is_not_asked_either(store, cal):
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED), NO_CREW])
    run(store, cal, llm)
    assert len(llm.calls) == 2


def test_the_policy_can_switch_asking_off(store, cal):
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove"), NO_CREW])
    _, st = run(store, cal, llm, policy=Policy(ask_if_unscheduled=False))
    assert len(llm.calls) == 2 and st["schedule"]["output"]["decision"] == "continue_to_tier1"


def test_two_candidate_dates_are_both_shown_and_one_match_is_enough(store, cal):
    cal.seed(date(2026, 11, 2), 34, "1800", "0600")  # Monday: this week's, and the 9th is empty
    llm = FakeLlm([intent_reply(True, 0.9, dates=[{"weekday": "monday"}], direction="remove"), NO_CREW])
    _, st = run(store, cal, llm)
    days = st["schedule"]["output"]["days"]
    assert [d["date"] for d in days] == ["2026-11-02", "2026-11-09"] and [d["sender_squad_scheduled"] for d in days] == [True, False]
    assert len(llm.calls) == 2  # scheduled on one of them: carry on


def test_no_dates_or_no_calendar_means_no_lookup(store, cal):
    llm = FakeLlm([intent_reply(True, 0.9, direction="remove"), NO_CREW])
    _, st = run(store, cal, llm)
    assert "schedule" not in st and llm.calls[1]["user"].endswith("Calendar for those dates: not looked up")
    store.start_trace("t2", "replay", "u", "x")
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove"), NO_CREW])
    handle(event().model_copy(update={"trace_id": "t2"}), store, llm, intent_model=INTENT, calendar=None, policy=ASK)
    assert "schedule" not in {s["name"] for s in store.get_trace("t2")["steps"]} and len(llm.calls) == 2


def test_an_unknown_sender_squad_is_never_asked(store, cal):
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove"), NO_CREW])
    _, st = run(store, cal, llm, squad=None)
    assert len(llm.calls) == 2 and st["schedule"]["output"]["sender_squad"] is None
    assert "[sender's squad" not in llm.calls[1]["user"]


class Broken(MockCalendar):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def get_day(self, shift_date):
        raise self.exc


def test_a_calendar_error_lets_the_message_continue_but_an_outage_is_retried(store):
    llm = FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove"), NO_CREW])
    run(store, Broken(CalendarError("bad")), llm)
    assert len(llm.calls) == 2  # fail open
    store.start_trace("t3", "replay", "u", "x")
    with pytest.raises(CalendarUnavailable):
        handle(event().model_copy(update={"trace_id": "t3"}), store, FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove")]),
               intent_model=INTENT, calendar=Broken(CalendarUnavailable("down")), policy=ASK)


def test_each_day_is_fetched_once_per_message(store, cal):
    cal.seed(WEDNESDAY, 34, "1800", "0000")
    run(store, cal, FakeLlm([intent_reply(True, 0.9, dates=WED, direction="remove"), NO_CREW]))
    assert [c for c in cal.calls if c[0] == "get_day"] == [("get_day", WEDNESDAY)]  # the validate stage reuses the lookup


def test_the_committed_policy_asks_and_an_old_intent_answer_still_parses():
    from chatbot.agent import intent
    from chatbot.policy import load_policy
    assert load_policy().ask_if_unscheduled is True
    assert intent.parse('{"is_schedule_change": true, "confidence": 0.9, "reason": "r"}').direction == "unclear"
