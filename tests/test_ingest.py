import re
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from chatbot.config import Settings, load_allowlist, load_roster
from chatbot.ingest.app import create_app
from chatbot.models import Event, RedactedText
from chatbot.pii.filter import PiiFilter
from chatbot.sqs import InMemoryPublisher
from chatbot.tracing.store import TraceStore
from tests.pii_cases import LEAKS


def settings(tmp_path, token=None):
    base = Settings.from_env()
    return Settings(str(tmp_path / "trace.db"), base.queue_url, None, base.roster_path, token)


@pytest.fixture
def env(tmp_path):
    s = settings(tmp_path)
    store, pub = TraceStore(s.trace_db_path), InMemoryPublisher()
    app = create_app(s, store, pub)
    return TestClient(app), store, pub, tmp_path


def msg(id="m1", text="54 won't have a crew Wednesday 2200-0100", created_at=1741115400, sender_id="1000002", **kw):
    return {"id": id, "created_at": created_at, "group_id": "g1", "sender_id": sender_id, "sender_type": "user",
            "name": "Sam Sample", "text": text, "system": False, "attachments": [], **kw}


def test_message_is_traced_and_enqueued(env):
    client, store, pub, _ = env
    r = client.post("/webhook", json=msg())
    assert r.json() == {"status": "enqueued", "trace_id": "m1"}
    [event] = pub.events()
    assert event["member"] == "Member-2" and event["squad"] == "54"
    assert event["text"] == "54 won't have a crew Wednesday 2200-0100"
    t = store.get_trace("m1")
    assert t["status"] == "enqueued" and t["source"] == "live"
    assert [s["name"] for s in t["steps"]] == ["inbound", "pii_filter", "enqueue"]
    assert t["steps"][0]["output"]["text_length"] == len("54 won't have a crew Wednesday 2200-0100")


def test_sent_at_comes_from_created_at_in_group_timezone(env):
    client, _, pub, _ = env
    client.post("/webhook", json=msg(created_at=1741115400))  # 2025-03-04 19:10:00 UTC
    sent_at = datetime.fromisoformat(pub.events()[0]["sent_at"])
    assert sent_at.isoformat() == "2025-03-04T14:10:00-05:00"


def test_replay_source_header(env):
    client, store, pub, _ = env
    client.post("/webhook", json=msg(), headers={"X-Source": "replay"})
    assert pub.events()[0]["source"] == "replay" and store.get_trace("m1")["source"] == "replay"
    assert client.post("/webhook", json=msg(id="m2"), headers={"X-Source": "bogus"}).status_code == 422


def test_duplicate_delivery_is_not_enqueued_twice(env):
    client, _, pub, _ = env
    client.post("/webhook", json=msg())
    assert client.post("/webhook", json=msg()).json()["status"] == "duplicate"
    assert len(pub.bodies) == 1


def test_dropped_message_is_not_enqueued_and_leaves_no_text(env):
    client, store, pub, _ = env
    r = client.post("/webhook", json=msg(text="Pt is a 72 yo male with chest pain"))
    assert r.json()["status"] == "dropped" and pub.bodies == []
    t = store.get_trace("m1")
    assert t["status"] == "filtered_dropped"
    assert t["steps"][1]["output"]["text"] is None


def test_bot_and_system_messages_are_ignored(env):
    client, _, pub, _ = env
    assert client.post("/webhook", json=msg(sender_type="bot")).json()["status"] == "ignored"
    assert client.post("/webhook", json=msg(id="m2", system=True)).json()["status"] == "ignored"
    assert pub.bodies == []


def test_queue_failure_returns_503_then_retry_succeeds(env):
    client, store, pub, _ = env
    pub.fail = True
    assert client.post("/webhook", json=msg()).status_code == 503
    assert store.get_trace("m1")["status"] == "enqueue_failed"
    pub.fail = False
    assert client.post("/webhook", json=msg()).json()["status"] == "enqueued"
    assert len(pub.bodies) == 1


def test_token_required_when_configured(tmp_path):
    s = settings(tmp_path, token="s3cret")
    client = TestClient(create_app(s, TraceStore(s.trace_db_path), InMemoryPublisher()))
    assert client.post("/webhook", json=msg()).status_code == 401
    assert client.post("/webhook?token=wrong", json=msg()).status_code == 401
    assert client.post("/webhook?token=s3cret", json=msg()).status_code == 200


def test_validation_error_does_not_echo_input(env):
    client, *_ = env
    r = client.post("/webhook", json={"id": "m1", "created_at": "not-a-number", "text": "Patient John Smith 72 yo"})
    assert r.status_code == 422 and "John" not in r.text and "72" not in r.text


def test_event_rejects_plain_text():
    with pytest.raises(ValidationError):
        Event(trace_id="t", source="live", group_id="g", sender_id="s", member=None, squad=None,
              sent_at=datetime.fromisoformat("2025-03-04T14:30:00-05:00"), text="raw text", pii_outcome="pass")


def test_event_json_round_trip_is_trusted():
    body = '{"trace_id":"t","source":"live","group_id":"g","sender_id":"s","member":null,"squad":null,' \
           '"sent_at":"2025-03-04T14:30:00-05:00","text":"54 no crew","pii_outcome":"pass","pii_redactions":{}}'
    assert isinstance(Event.model_validate_json(body).text, RedactedText)


def test_event_keeps_redacted_text_type():
    event = Event(trace_id="t", source="live", group_id="g", sender_id="s", member=None, squad=None,
                  sent_at=datetime.fromisoformat("2025-03-04T14:30:00-05:00"), text=RedactedText.mint("x"), pii_outcome="pass")
    assert isinstance(event.text, RedactedText)


@pytest.mark.parametrize("text,forbidden", LEAKS)
def test_no_leak_anywhere_after_full_ingest(env, text, forbidden):
    """CI leak test: raw PII must not appear in the queue body or in any byte of the trace database."""
    client, _, pub, tmp_path = env
    r = client.post("/webhook", json=msg(text=text))
    assert r.status_code == 200
    haystacks = list(pub.bodies) + [p.read_bytes().decode("latin-1") for p in Path(tmp_path).glob("trace.db*")]
    for f in forbidden:
        for h in haystacks:
            assert not re.search(r"(?<!\w)" + re.escape(f) + r"(?!\w)", h, re.IGNORECASE), f"{f!r} leaked"
