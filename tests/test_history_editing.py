import json

import pytest
from fastapi.testclient import TestClient

import chatbot.replay.app as replay_app
from chatbot.config import Settings
from chatbot.replay.app import create_app as create_replay
from chatbot.tracing.store import TraceStore

ROW = {"kind": "member", "squad": "54", "speaker": "Member-2", "addressee": None,
       "sent_at": "2026-11-02T13:50:00", "text": "Anyone covering Wednesday night?"}


@pytest.fixture
def replay(tmp_path):
    base = Settings.from_env()
    s = Settings(str(tmp_path / "trace.db"), base.queue_url, None, base.roster_path, None)
    store = TraceStore(tmp_path / "history")
    return TestClient(create_replay(s, store, post=lambda p: {"status": "enqueued", "trace_id": "x"})), store


def test_add_list_and_preview_history(replay):
    client, _ = replay
    assert client.post("/api/history", json=ROW).status_code == 200
    rows = client.get("/api/history", params={"squad": "54"}).json()
    assert [r["text"] for r in rows] == ["Anyone covering Wednesday night?"]
    preview = client.get("/api/history/preview", params={"squad": "54", "until": "2026-11-02T14:10:00"}).json()["text"]
    assert "Member-2 (squad 54): Anyone covering Wednesday night?" in preview
    assert client.get("/api/history", params={"squad": "34"}).json() == []


def test_edit_keeps_the_stored_row_and_delete_removes_it(replay):
    client, store = replay
    row_id = client.post("/api/history", json=ROW).json()["id"]
    edited = {**ROW, "text": "Anyone covering Thursday?", "sent_at": "2026-11-02T13:55:00"}
    assert client.put(f"/api/history/{row_id}", json=edited).status_code == 200
    [row] = store.list_messages("54")
    assert (row["text"], row["sent_at"].minute) == ("Anyone covering Thursday?", 55)
    assert client.delete(f"/api/history/{row_id}").status_code == 200
    assert store.list_messages("54") == []
    assert client.delete(f"/api/history/{row_id}").status_code == 404


def test_a_bot_line_is_from_bot_to_a_roster_pseudonym(replay):
    client, store = replay
    bot = {"kind": "bot", "squad": "54", "speaker": "bot", "addressee": "Member-2",
           "sent_at": "2026-11-02T13:51:00", "text": "Wednesday is covered."}
    assert client.post("/api/history", json=bot).status_code == 200
    assert client.post("/api/history", json={**bot, "addressee": "Nobody"}).status_code == 422
    assert client.post("/api/history", json={**ROW, "speaker": "Nobody"}).status_code == 422
    assert client.post("/api/history", json={**ROW, "kind": "robot"}).status_code == 422
    assert [r["kind"] for r in store.list_messages("54")] == ["bot"]


def test_empty_text_is_refused(replay):
    client, _ = replay
    assert client.post("/api/history", json={**ROW, "text": "   "}).status_code == 422


def test_clear_history_needs_its_phrase_and_leaves_traces_alone(replay):
    client, store = replay
    store.start_trace("t1", "replay", "u", "x")
    client.post("/api/history", json=ROW)
    assert client.post("/api/history/clear", json={"confirm": "yes"}).status_code == 422
    assert client.post("/api/history/clear", json={"confirm": "clear history"}).status_code == 200
    assert store.list_messages() == []
    assert store.get_trace("t1") is not None


def test_scenario_history_loads_as_a_whole_or_not_at_all(replay, tmp_path, monkeypatch):
    client, store = replay
    monkeypatch.setattr(replay_app, "SCENARIOS", tmp_path)
    good = {"kind": "member", "squad": "54", "speaker": "Member-2", "sent_at": "2026-11-02T13:50:00-05:00", "text": "first"}
    bad = {**good, "speaker": "Nobody", "text": "second"}
    (tmp_path / "mixed.json").write_text(json.dumps({"history": [good, bad], "messages": []}))
    (tmp_path / "fine.json").write_text(json.dumps({"description": "d", "history": [good], "messages": [
        {"member": "Member-2", "sent_at": "2026-11-02T14:10:00-05:00", "text": "54 is out"}]}))
    assert client.post("/api/scenarios/mixed/load-history").status_code == 422
    assert store.list_messages() == []  # nothing stored from a scenario with a bad row
    assert [s["name"] for s in client.get("/api/scenarios").json()] == ["fine", "mixed"]
    assert client.post("/api/scenarios/fine/load-history").json() == {"added": 1}
    assert client.get("/api/scenarios/fine").json()["messages"][0]["member"] == "Member-2"
    assert client.get("/api/scenarios/../etc").status_code == 404
