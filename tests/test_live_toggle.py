from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from chatbot.agent.worker import handle
from chatbot.config import Settings
from chatbot.ingest.app import create_app as create_ingest
from chatbot.models import Event, RedactedText
from chatbot.policy import Policy
from chatbot.replay.app import create_app as create_replay
from chatbot.schedule.mock import MockCalendar
from chatbot.sqs import InMemoryPublisher
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, cmd, reply

MONDAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def allow_writes(monkeypatch):
    monkeypatch.setenv("CALENDAR_ALLOW_WRITES", "1")


@pytest.fixture
def stack(tmp_path):
    base = Settings.from_env()
    s = Settings(str(tmp_path / "trace.db"), base.queue_url, None, base.roster_path, None)
    store, pub = TraceStore(s.trace_db_path), InMemoryPublisher()
    ingest = TestClient(create_ingest(s, store, pub))
    replay = TestClient(create_replay(s, store, post=lambda p: ingest.post("/webhook", json=p, headers={"X-Source": "replay"}).json()))
    return replay, pub


def send(replay, **extra):
    return replay.post("/api/send", json={"sender_id": "1000002", "text": "54 won't have a crew tonight 2200-0100",
                                          "sent_at": "2025-03-04T14:10:00", **extra})


def test_the_toggle_is_on_by_default_and_travels_with_the_message(stack):
    replay, pub = stack
    send(replay)
    assert Event.model_validate_json(pub.bodies[-1]).live_calendar is True
    send(replay, live_calendar=False)
    assert Event.model_validate_json(pub.bodies[-1]).live_calendar is False


def test_a_message_from_the_replay_tool_cannot_be_forced_into_a_live_write_by_a_missing_flag(stack):
    replay, pub = stack
    send(replay)
    body = pub.bodies[-1]
    assert '"live_calendar"' in body  # always stated on the queue, never implied


def test_an_off_message_is_only_previewed_even_when_the_policy_is_live(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    store.start_trace("t1", "replay", "u", "x")
    cal = MockCalendar()
    cal.seed(MONDAY, 54, "1800", "0000")
    event = Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad="54",
                  sent_at=datetime.fromisoformat("2026-11-02T14:10:00-05:00"), text=RedactedText.mint("54 short"),
                  pii_outcome="pass", live_calendar=False)
    llm = FakeLlm(reply(commands=[cmd("NoCrew", ["54"], "2026-11-02", "2200", "0100")]))
    handle(event, store, llm, calendar=cal, policy=Policy(mode="live"))
    apply = {s["name"]: s for s in store.get_trace("t1")["steps"]}["apply"]
    assert apply["output"]["mode"] == "dry_run" and apply["output"]["live_calendar"] is False
    assert not [c for c in cal.calls if c[0] == "noCrew" and c[-1] is False]  # no real write
