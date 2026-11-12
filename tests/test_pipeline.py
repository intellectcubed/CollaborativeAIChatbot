from datetime import datetime

import pytest

from chatbot.agent.pipeline import AgentConfig, Context, Stage, run_pipeline
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import TraceStore


@pytest.fixture
def ctx(tmp_path):
    store = TraceStore(tmp_path / "t.db")
    store.start_trace("t1", "replay", "u", "x")
    event = Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member=None, squad=None,
                  sent_at=datetime.fromisoformat("2025-03-05T14:10:00-05:00"), text=RedactedText.mint("hi"), pii_outcome="pass")
    return Context(event, store, llm=None, config=AgentConfig())


class Recorder(Stage):
    def __init__(self, name, **kw):
        self.name, self.calls = name, 0
        self.__dict__.update(kw)

    def run(self, ctx, step):
        self.calls += 1
        step.output = {"ran": self.name}
        if getattr(self, "stop_with", None):
            ctx.outcome = self.stop_with
        if getattr(self, "boom", None):
            raise self.boom


def names(ctx):
    return [s["name"] for s in ctx.store.get_trace("t1")["steps"]]


def test_stages_run_in_order_and_finish_as_done(ctx):
    run_pipeline([Recorder("a"), Recorder("b")], ctx)
    assert names(ctx) == ["a", "b"] and ctx.store.get_trace("t1")["status"] == "done"


def test_a_stage_can_stop_the_pipeline(ctx):
    later = Recorder("later")
    run_pipeline([Recorder("a", stop_with="ignored"), later], ctx)
    assert later.calls == 0 and ctx.store.get_trace("t1")["status"] == "ignored"


def test_untraced_stage_leaves_no_step_and_duplicate_leaves_status_alone(ctx):
    ctx.store.set_status("t1", "done")
    run_pipeline([Recorder("d", traced=False, stop_with="duplicate"), Recorder("a")], ctx)
    assert names(ctx) == [] and ctx.store.get_trace("t1")["status"] == "done"


def test_stage_that_does_not_apply_is_skipped(ctx):
    skipped = Recorder("s", applies=lambda c: False)
    run_pipeline([skipped, Recorder("b")], ctx)
    assert skipped.calls == 0 and names(ctx) == ["b"]


def test_failure_ends_as_error_unless_the_stage_fails_open(ctx):
    run_pipeline([Recorder("a", boom=ValueError()), Recorder("b")], ctx)
    assert ctx.store.get_trace("t1")["status"] == "error" and names(ctx) == ["a"]


def test_fail_open_stage_lets_the_message_continue(ctx):
    run_pipeline([Recorder("a", boom=ValueError(), fail_open=True), Recorder("b")], ctx)
    assert names(ctx) == ["a", "b"] and ctx.store.get_trace("t1")["status"] == "done"


def test_transient_failure_propagates_for_a_queue_retry(ctx):
    with pytest.raises(ConnectionError):
        run_pipeline([Recorder("a", boom=ConnectionError())], ctx)
    assert ctx.store.get_trace("t1")["status"] == "received"
