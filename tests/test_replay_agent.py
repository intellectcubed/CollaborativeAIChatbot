from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from chatbot.agent.worker import handle
from chatbot.config import Settings
from chatbot.ingest.app import create_app as create_ingest
from chatbot.models import Event
from tests.fakes import FakeLlm, NO_CREW_JSON
from chatbot.replay.app import create_app as create_replay
from chatbot.sqs import InMemoryPublisher
from chatbot.tracing.store import TraceStore


@pytest.fixture
def stack(tmp_path):
    base = Settings.from_env()
    s = Settings(str(tmp_path / "trace.db"), base.queue_url, None, base.roster_path, None)
    store, pub = TraceStore(s.trace_db_path), InMemoryPublisher()
    ingest = TestClient(create_ingest(s, store, pub))

    def post(payload):
        return ingest.post("/webhook", json=payload, headers={"X-Source": "replay"}).json()

    return TestClient(create_replay(s, store, post=post)), store, pub


def test_replay_to_agent_end_to_end(stack):
    replay, store, pub = stack
    r = replay.post("/api/send", json={"sender_id": "1000002", "text": "54 won't have a crew tonight",
                                       "sent_at": "2025-03-04T14:10:00"})
    assert r.json()["status"] == "enqueued"
    trace_id = r.json()["trace_id"]

    handle(Event.model_validate_json(pub.bodies[0]), store, FakeLlm(NO_CREW_JSON))

    t = replay.get(f"/api/traces/{trace_id}").json()
    assert t["status"] == "done" and t["source"] == "replay"
    assert [s["name"] for s in t["steps"]] == ["inbound", "pii_filter", "enqueue", "agent_received", "llm_tier1", "interpret", "reply"]
    assert t["sent_at"] == "2025-03-04T14:10:00-05:00"
    assert t["steps"][-1]["output"]["lines"] == ["Call NoCrew on calendar for date 2025-03-05 (squad 54, 2200-0100)"]


def test_replay_unknown_sender_and_members_list(stack):
    replay, *_ = stack
    assert replay.post("/api/send", json={"sender_id": "nope", "text": "x"}).status_code == 404
    members = replay.get("/api/members").json()
    assert {"sender_id": "1000002", "member": "Member-2", "squad": "54", "name": "Sam Sample"} in members


def test_replay_serves_page(stack):
    replay, *_ = stack
    assert "Replay tool" in replay.get("/").text




def test_chat_send_posts_to_the_processor_exactly_once(tmp_path):
    base = Settings.from_env()
    s = Settings(str(tmp_path / "trace.db"), base.queue_url, None, base.roster_path, None)
    posted = []
    replay = TestClient(create_replay(s, TraceStore(s.trace_db_path), post=lambda p: posted.append(p) or {"status": "enqueued", "trace_id": p["id"]}))
    replay.post("/api/send", json={"sender_id": "1000002", "text": "54 no crew tonight"})
    assert len(posted) == 1
