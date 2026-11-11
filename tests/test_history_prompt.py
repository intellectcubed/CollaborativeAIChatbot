from datetime import datetime, timedelta, timezone

import pytest

from chatbot.agent.history import MAX_EARLIER, MAX_LINES, NO_HISTORY, label, render
from chatbot.agent.worker import handle
from chatbot.channels import Channels
from chatbot.channels.builtin import LogChannel, ReplayChannel
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, cmd, intent_reply, reply

NOW = datetime(2026, 11, 2, 14, 10, tzinfo=timezone.utc)
CHANNELS = Channels({"replay": ReplayChannel(), "log": LogChannel()}, {"live": "log", "replay": "replay"})


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "prompt")
    s.start_trace("t1", "replay", "u", "x")
    s.record_message(kind="member", sent_at=NOW - timedelta(minutes=30), squad="54", speaker="Member-2",
                     text=RedactedText.mint("we are short tonight"), source="replay", trace_id="t0")
    s.record_message(kind="bot", sent_at=NOW - timedelta(minutes=20), squad="54", speaker="bot", addressee="Member-1",
                     text=RedactedText.mint("Did you mean this Saturday?"), source="replay", trace_id="t0")
    s.record_message(kind="member", sent_at=NOW - timedelta(minutes=10), squad="34", speaker="Member-9",
                     text=RedactedText.mint("another squad, not ours"), source="replay", trace_id="t9")
    return s


def event():
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad="54",
                 sent_at=NOW, text=RedactedText.mint("yes, the Saturday one"), pii_outcome="pass")


def test_the_bot_label_is_shown_in_the_prompt_and_other_squads_last_hour_are_included(store):
    llm = FakeLlm([intent_reply(True, 0.9), reply(commands=[], confidence=0.9)])
    handle(event(), store, llm, intent_model="claude-haiku-5-5", channels=CHANNELS)
    for call in llm.calls:
        assert "bot → Member-1 (squad 54): Did you mean this Saturday?" in call["user"]
        assert "Member-2 (squad 54): we are short tonight" in call["user"]
        assert "another squad, not ours" in call["user"]  # within the hour, every squad is context
        assert "(the message above is not part of it)" in call["user"] or "(the message below is not part of it)" in call["user"]


def test_the_chat_gets_only_the_plain_message(store):
    llm = FakeLlm([intent_reply(True, 0.9), reply(commands=[cmd("Clarify", question="Which day?")], confidence=0.9)])
    handle(event(), store, llm, intent_model="claude-haiku-5-5", channels=CHANNELS)
    [row] = store.get_outbox("t1")
    assert row["text"] == "Which day?" and "bot →" not in row["text"]  # the chat sees the question, nothing else
    bot_rows = [r for r in store.history("54", until=NOW + timedelta(seconds=1)) if r["kind"] == "bot" and r["trace_id"] == "t1"]
    assert [r["text"] for r in bot_rows] == ["Which day?"]  # stored plain too; the label is added only in the prompt


def test_labels():
    assert label({"kind": "member", "speaker": "Member-3", "squad": "42"}) == "Member-3 (squad 42)"
    assert label({"kind": "bot", "addressee": "Member-3", "squad": "42"}) == "bot → Member-3 (squad 42)"
    assert label({"kind": "bot", "addressee": None, "squad": "42"}) == "bot → a member (squad 42)"


def test_recent_and_earlier_are_separate_sections():
    rows = [
        {"sent_at": NOW - timedelta(hours=3), "kind": "member", "speaker": "M1", "squad": "54", "text": "old"},
        {"sent_at": NOW - timedelta(minutes=5), "kind": "member", "speaker": "M2", "squad": "54", "text": "fresh"},
    ]
    text = render(rows, NOW)
    assert text.index("Most important") < text.index("fresh") < text.index("Less important") < text.index("old")


def test_the_headers_are_bold_and_each_section_is_labelled_as_history():
    rows = [
        {"sent_at": NOW - timedelta(hours=3), "kind": "member", "speaker": "M1", "squad": "54", "text": "old"},
        {"sent_at": NOW - timedelta(minutes=5), "kind": "member", "speaker": "M2", "squad": "54", "text": "fresh"},
    ]
    lines = render(rows, NOW).split("\n")
    assert lines[0] == "Chat history:"
    assert lines[1] == "**Most important (last hour, all squads; newest last, and the newest lines matter most):**"
    assert lines.index("Chat history:", 1) < lines.index("**Less important (this squad, older background):**")


def test_only_the_10_newest_older_background_lines_are_kept():
    rows = [{"sent_at": NOW - timedelta(hours=2, minutes=i), "kind": "member", "speaker": "M", "squad": "54",
             "text": f"old{i}"} for i in range(MAX_EARLIER + 5)]
    text = render(sorted(rows, key=lambda r: r["sent_at"]), NOW)
    assert "(5 older background messages not shown)" in text
    assert "old0" in text and f"old{MAX_EARLIER - 1}" in text and f"old{MAX_EARLIER}" not in text


def test_no_messages_says_so():
    assert render([], NOW) == NO_HISTORY


def test_a_very_busy_chat_keeps_only_the_latest_lines():
    rows = [{"sent_at": NOW - timedelta(minutes=i), "kind": "member", "speaker": "M", "squad": "54", "text": f"m{i}"}
            for i in range(MAX_LINES + 10)]
    text = render(sorted(rows, key=lambda r: r["sent_at"]), NOW)
    assert "(10 older messages not shown)" in text and "m0" in text and f"m{MAX_LINES + 9}" not in text
