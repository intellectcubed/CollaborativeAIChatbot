from datetime import datetime, timedelta, timezone

import pytest

from chatbot.agent.worker import handle
from chatbot.channels import Channels
from chatbot.channels.builtin import LogChannel, ReplayChannel
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import HISTORY_HOURS, TraceStore
from tests.fakes import FakeLlm, cmd, reply

NOW = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "history")
    s.start_trace("t1", "replay", "u", "x")
    return s


def member(store, when, squad="54", speaker="Member-1", text="msg", trace=None, source="replay"):
    store.record_message(kind="member", sent_at=when, squad=squad, speaker=speaker, text=RedactedText.mint(text),
                         source=source, trace_id=trace)


def test_history_is_this_squad_for_the_window_and_every_squad_within_the_hour(store):
    member(store, NOW - timedelta(hours=49), text="too old")
    member(store, NOW - timedelta(hours=3), text="three hours ago")
    member(store, NOW - timedelta(minutes=5), text="five minutes ago")
    member(store, NOW - timedelta(minutes=1), squad="34", text="another squad, last hour")
    member(store, NOW - timedelta(hours=2), squad="34", text="another squad, two hours ago")
    member(store, NOW + timedelta(minutes=1), text="after the message")
    texts = [r["text"] for r in store.history("54", until=NOW)]
    assert texts == ["three hours ago", "five minutes ago", "another squad, last hour"]


def test_the_window_is_48_hours_from_the_message_time_not_from_now(store):
    member(store, NOW - timedelta(hours=HISTORY_HOURS), text="exactly four hours")
    member(store, NOW - timedelta(hours=HISTORY_HOURS + 1), text="beyond")
    assert [r["text"] for r in store.history("54", until=NOW)] == ["exactly four hours"]
    # the same messages, judged from a later message time, fall out of the window
    assert store.history("54", until=NOW + timedelta(hours=2)) == []


def test_the_message_being_answered_is_left_out(store):
    member(store, NOW - timedelta(minutes=2), text="earlier")
    member(store, NOW, text="this one", trace="t1")
    assert [r["text"] for r in store.history("54", until=NOW, exclude_trace="t1")] == ["earlier"]


def test_bot_posts_are_part_of_the_squad_history_with_their_addressee(store):
    store.record_message(kind="bot", sent_at=NOW - timedelta(minutes=3), squad="54", speaker="bot", addressee="Member-1",
                         text=RedactedText.mint("Did you mean this Saturday?"), source="replay", trace_id="t0")
    [row] = store.history("54", until=NOW)
    assert (row["kind"], row["speaker"], row["addressee"]) == ("bot", "bot", "Member-1")


def test_chat_history_refuses_text_that_is_not_redacted(store):
    with pytest.raises(TypeError):
        store.record_message(kind="member", sent_at=NOW, squad="54", speaker="Member-1", text="plain",
                            source="replay")


def test_the_agent_records_a_bot_reply_only_when_it_was_delivered(tmp_path):
    channels = Channels({"replay": ReplayChannel(), "log": LogChannel()}, {"live": "log", "replay": "replay"})
    question = reply(commands=[cmd("Clarify", question="Which day?")], confidence=0.9)

    replay_store = TraceStore(tmp_path / "replay")
    replay_store.start_trace("t1", "replay", "u", "x")
    handle(event(), replay_store, FakeLlm(question), channels=channels)
    [bot] = [r for r in replay_store.history("54", until=NOW + timedelta(seconds=1)) if r["kind"] == "bot"]
    assert (bot["text"], bot["addressee"]) == ("Which day?", "Member-1")

    live_store = TraceStore(tmp_path / "live")
    live_store.start_trace("t1", "live", "u", "x")
    handle(event().model_copy(update={"source": "live"}), live_store, FakeLlm(question), channels=channels)
    assert not [r for r in live_store.history("54", until=NOW + timedelta(seconds=1)) if r["kind"] == "bot"]


def event():
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad="54",
                 sent_at=NOW, text=RedactedText.mint("54 short"), pii_outcome="pass")


def test_clear_empties_every_table_of_the_store(store):
    member(store, NOW)
    store.outbox_begin("t1", 1, "clarify", "Member-1", RedactedText.mint("Which day?"), "54")
    store.clear()
    assert store.get_trace("t1") is None and store.history("54", until=NOW + timedelta(hours=1)) == []
    assert store.get_outbox("t1") == [] and store.list_traces() == []


def test_clear_is_refused_for_the_production_database():
    from chatbot.tracing.store import refuse_if_production
    refuse_if_production("postgresql://chatbot:x@localhost:5432/chatbot_test")  # the test database: allowed
    with pytest.raises(RuntimeError, match="production"):
        refuse_if_production("postgresql://chatbot:x@localhost:5432/chatbot_prod")


def test_the_replay_tool_clears_only_with_the_confirmation_phrase(store):
    from fastapi.testclient import TestClient
    from chatbot.config import Settings
    from chatbot.replay.app import create_app as create_replay
    base = Settings.from_env()
    client = TestClient(create_replay(Settings("unused", base.queue_url, None, base.roster_path, None), store, post=lambda p: {}))
    member(store, NOW)
    assert client.post("/api/clear", json={"confirm": "yes"}).status_code == 422
    assert store.history("54", until=NOW + timedelta(hours=1))  # nothing cleared
    assert client.post("/api/clear", json={"confirm": "clear test data"}).json() == {"cleared": True}
    assert store.history("54", until=NOW + timedelta(hours=1)) == []
