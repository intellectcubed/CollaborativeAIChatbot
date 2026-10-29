import multiprocessing as mp

import pytest

from chatbot.models import RedactedText
from chatbot.tracing.store import TraceStore, UnredactedTextError


@pytest.fixture
def store(tmp_path):
    return TraceStore(tmp_path / "trace.db")


def test_steps_come_back_in_order(store):
    assert store.start_trace("t1", "live", "u1", "2025-03-04T14:10:00-05:00")
    with store.step("t1", "inbound") as s:
        s.output = {"length": 12}
    with store.step("t1", "pii_filter") as s:
        s.input = {"text": RedactedText.mint("54 no crew " * 20)}
        s.output = {"outcome": "pass"}
    t = store.get_trace("t1")
    assert [x["name"] for x in t["steps"]] == ["inbound", "pii_filter"]
    assert [x["seq"] for x in t["steps"]] == [1, 2]
    assert t["steps"][1]["output"] == {"outcome": "pass"}


def test_error_records_exception_type_only(store):
    store.start_trace("t1", "live", "u1", "x")
    with pytest.raises(KeyError):
        with store.step("t1", "tier1"):
            raise KeyError("secret message text")
    step = store.get_trace("t1")["steps"][0]
    assert step["error"] == "KeyError"


def test_plain_long_string_is_rejected(store):
    store.start_trace("t1", "live", "u1", "x")
    with pytest.raises(UnredactedTextError):
        with store.step("t1", "tier1") as s:
            s.input = {"prompt": "x" * 200}
    with pytest.raises(UnredactedTextError):
        with store.step("t1", "tier1") as s:
            s.output = {"nested": [{"raw": "y" * 65}]}
    assert store.get_trace("t1")["steps"] == []


def test_duplicate_delivery(store):
    assert store.start_trace("t1", "live", "u1", "x") is True
    assert store.start_trace("t1", "live", "u1", "x") is True  # still 'received': retry allowed
    store.set_status("t1", "enqueued")
    assert store.start_trace("t1", "live", "u1", "x") is False


def _writer(path, trace_id, n):
    store = TraceStore(path)
    for i in range(n):
        with store.step(trace_id, f"s{i}") as s:
            s.output = {"i": i}


def test_two_processes_write_one_trace(tmp_path):
    path = tmp_path / "trace.db"
    store = TraceStore(path)
    store.start_trace("t1", "live", "u1", "x")
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_writer, args=(str(path), "t1", 25)) for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    steps = store.get_trace("t1")["steps"]
    assert len(steps) == 50
    assert [s["seq"] for s in steps] == list(range(1, 51))
