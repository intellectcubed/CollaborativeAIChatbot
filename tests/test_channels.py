from datetime import date, datetime

import pytest

from chatbot.agent.worker import handle
from chatbot.channels import Channels, OutboundMessage, SendResult, load_channels
from chatbot.channels.builtin import LogChannel, ReplayChannel
from chatbot.models import Event, RedactedText
from chatbot.policy import Policy, load_policy
from chatbot.schedule.mock import MockCalendar
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, cmd, reply

MONDAY = date(2026, 11, 2)
ROUTES = {"live": "log", "replay": "replay"}


class Recorder:
    name = "replay"

    def __init__(self, exc=None):
        self.sent, self.exc = [], exc

    def send(self, msg):
        if self.exc:
            raise self.exc
        self.sent.append(msg)
        return SendResult("sent")


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "t.db")
    s.start_trace("t1", "replay", "u", "x")
    return s


@pytest.fixture
def cal():
    c = MockCalendar()
    c.seed(MONDAY, 42, "1800", "0600")
    return c


@pytest.fixture(autouse=True)
def allow_writes(monkeypatch):
    monkeypatch.setenv("CALENDAR_ALLOW_WRITES", "1")


def event(source="replay"):
    return Event(trace_id="t1", source=source, group_id="g", sender_id="u", member="Member-1", squad="42",
                 sent_at=datetime.fromisoformat("2026-11-02T14:10:00-05:00"), text=RedactedText.mint("msg"), pii_outcome="pass")


def run(store, commands, cal=None, source="replay", channels=None, policy=Policy(), confidence=0.95):
    channels = channels or Channels({"replay": ReplayChannel(), "log": LogChannel()}, ROUTES)
    handle(event(source), store, FakeLlm(reply(commands=commands, confidence=confidence)), calendar=cal, policy=policy, channels=channels)
    return store.get_trace("t1"), store.get_outbox("t1")


QUESTION = cmd("NoCrew", ["42"], "2026-11-02", "2200", "0100")


def test_clarify_question_goes_to_outbox_and_replay_channel(store, cal):
    trace, rows = run(store, [cmd("NoCrew", ["42"], "", "2200", "0100")], cal)
    [row] = rows
    assert (row["kind"], row["channel"], row["status"], row["to_member"]) == ("clarify", "replay", "sent", "Member-1")
    assert row["text"] == "Which date is that for squad 42?"  # the "Ask sender:" prefix is not part of the reply
    assert [s["name"] for s in trace["steps"]].index("respond") < [s["name"] for s in trace["steps"]].index("reply")


def test_live_source_is_routed_to_log_and_still_recorded(store, cal):
    _, rows = run(store, [cmd("NoCrew", ["42"], "", "2200", "0100")], cal, source="live")
    [row] = rows
    assert (row["channel"], row["status"], row["sent_at"]) == ("log", "skipped", None)


def test_the_source_picks_the_channel(store, cal):
    live, replay = Recorder(), Recorder()
    live.name = "groupme"
    ch = Channels({"groupme": live, "replay": replay, "log": LogChannel()}, {"live": "groupme", "replay": "replay"})
    run(store, [cmd("NoCrew", ["42"], "", "2200", "0100")], cal, source="live", channels=ch)
    assert len(live.sent) == 1 and not replay.sent


def test_results_are_announced_only_when_the_policy_says_so(store, cal):
    _, rows = run(store, [QUESTION], cal, policy=Policy(announce_results=False))
    assert rows == []
    store.start_trace("t2", "replay", "u", "x")  # a second message, same cal
    e = event().model_copy(update={"trace_id": "t2"})
    handle(e, store, FakeLlm(reply(commands=[QUESTION])), calendar=cal, policy=Policy(announce_results=True),
           channels=Channels({"replay": ReplayChannel()}, ROUTES))
    [row] = store.get_outbox("t2")
    assert row["kind"] == "result" and row["text"].startswith("Would call NoCrew squad 42")


def test_low_confidence_is_recorded_for_the_admin_but_not_posted(store, cal):
    rec = Recorder()
    _, rows = run(store, [QUESTION], cal, confidence=0.3, channels=Channels({"replay": rec}, ROUTES))
    [row] = rows
    assert (row["kind"], row["channel"], row["status"]) == ("flag", "-", "skipped") and not rec.sent


def test_a_failed_send_is_recorded_and_does_not_change_the_outcome(store, cal):
    trace, rows = run(store, [cmd("NoCrew", ["42"], "", "2200", "0100")], cal,
                      channels=Channels({"replay": Recorder(exc=ConnectionError())}, ROUTES))
    assert (rows[0]["status"], rows[0]["error"]) == ("failed", "ConnectionError")
    assert trace["status"] == "done"


def test_nothing_is_sent_twice_for_the_same_message(store):
    assert store.outbox_begin("t1", 1, "clarify", "Member-1", RedactedText.mint("Which date?"))
    store.outbox_finish("t1", 1, "replay", "sent")
    assert not store.outbox_begin("t1", 1, "clarify", "Member-1", RedactedText.mint("Which date?"))
    store.outbox_finish("t1", 2, "replay", "failed", "Timeout")  # no row yet: harmless
    store.outbox_begin("t1", 3, "clarify", "Member-1", RedactedText.mint("x"))
    store.outbox_finish("t1", 3, "replay", "failed", "Timeout")
    assert store.outbox_begin("t1", 3, "clarify", "Member-1", RedactedText.mint("x"))  # a failed send may be retried


def test_the_outbox_refuses_unredacted_long_text(store):
    with pytest.raises(ValueError):
        store.outbox_begin("t1", 1, "clarify", "Member-1", "x" * 200)


def test_without_a_calendar_the_models_own_question_is_sent(store):
    _, rows = run(store, [cmd("Clarify", question="Which night do you mean?")])
    assert [(r["kind"], r["text"]) for r in rows] == [("clarify", "Which night do you mean?")]


def test_routes_fall_back_to_safe_defaults(tmp_path):
    assert load_channels(tmp_path / "missing.toml").routes == {"live": "log", "replay": "replay"}
    bad = tmp_path / "c.toml"
    bad.write_text("[routes\n")
    assert load_channels(bad).routes == {"live": "log", "replay": "replay"}
    bad.write_text('[routes]\nlive = "nowhere"\n')
    assert load_channels(bad).for_source("live").name == "log"  # unknown channel -> log
    assert load_channels(bad).for_source("other").name == "log"


def test_announce_results_must_be_a_real_boolean(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text('mode = "live"\nannounce_results = "yes"\n')
    assert load_policy(p) == Policy()  # unusable file: safe defaults
    p.write_text("announce_results = true\n")
    assert load_policy(p).announce_results is True


def test_the_committed_config_is_safe():
    assert load_channels().routes["live"] == "log"
    assert load_policy().announce_results is False
