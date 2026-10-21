from datetime import datetime
from types import SimpleNamespace

import pytest

from chatbot.agent import interpret
from chatbot.agent.worker import handle
from chatbot.interfaces.llm import AnthropicClient, LlmRefusal, LlmTruncated, StubLlm
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, cmd, reply


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "t.db")
    s.start_trace("t1", "replay", "u", "x")
    return s


def event(text="54 won't have a crew tonight 2200-0100"):
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-2", squad="54",
                 sent_at=datetime.fromisoformat("2025-03-05T14:10:00-05:00"), text=RedactedText.mint(text), pii_outcome="pass")


def steps(store):
    t = store.get_trace("t1")
    return t["status"], {s["name"]: s for s in t["steps"]}


def test_prompt_carries_time_sender_and_redacted_text():
    p = interpret.build_user_message(event(), interpret.load_prompt())
    assert isinstance(p, RedactedText)
    assert "Wednesday 2025-03-05 14:10 EST" in p and "Member-2 (squad 54)" in p and "54 won't have a crew tonight" in p


def test_no_crew_renders_calendar_call_and_traces_prompt_and_response(store):
    llm = FakeLlm(reply(commands=[cmd("NoCrew", ["54"], "2025-03-05", "2200", "0100")], confidence=0.9))
    handle(event(), store, llm, model="claude-haiku-5-5")
    status, st = steps(store)
    assert status == "done"
    assert st["reply"]["output"]["lines"] == ["Call NoCrew on calendar for date 2025-03-05 (squad 54, 2200-0100)"]
    assert st["llm_tier1"]["input"]["user"].endswith("Message: 54 won't have a crew tonight 2200-0100")
    assert st["llm_tier1"]["input"]["model"] == "claude-haiku-5-5"
    assert st["llm_tier1"]["output"]["cost_usd"] == 0.00015  # 1000 in x $0.10/M + 100 out x $0.50/M
    assert st["interpret"]["output"]["confidence"] == 0.9
    assert llm.calls[0]["schema"] == interpret.load_prompt().schema
    assert st["llm_tier1"]["input"]["prompt_version"] == interpret.load_prompt().version
    assert st["llm_tier1"]["input"]["system"] == interpret.load_prompt().system


def test_get_shift_and_clarify_and_multiple_commands(store):
    handle(event(), store, FakeLlm(reply(commands=[
        cmd("GetShift", date="2025-03-07"),
        cmd("AddCrew", ["34", "42"], "2025-03-08", "1800", "0600"),
        cmd("Clarify", question="Which night do you mean?"),
    ])))
    lines = steps(store)[1]["reply"]["output"]["lines"]
    assert lines == [
        "Get shift for date 2025-03-07",
        "Call AddCrew on calendar for date 2025-03-08 (squad 34,42, 1800-0600)",
        "Ask sender: Which night do you mean?",
    ]


def test_not_scheduling_is_ignored(store):
    handle(event("Great job at training"), store, FakeLlm(reply(is_scheduling=False)))
    status, st = steps(store)
    assert status == "ignored" and st["reply"]["output"]["lines"] == ["no schedule action"]


@pytest.mark.parametrize("bad", ["not json", reply(commands=[cmd("NoCrew", ["54"], "tomorrow")]), '{"is_scheduling": true}'])
def test_unusable_answer_is_final_error_not_retry(store, bad):
    handle(event(), store, FakeLlm(bad))  # does not raise
    assert steps(store)[0] == "error"


@pytest.mark.parametrize("exc", [LlmRefusal("x"), LlmTruncated("x")])
def test_refusal_and_truncation_are_final_errors(store, exc):
    handle(event(), store, FakeLlm("", exc=exc))
    status, st = steps(store)
    assert status == "error" and st["llm_tier1"]["error"] == type(exc).__name__


def test_transient_failure_raises_so_the_queue_retries(store):
    with pytest.raises(ConnectionError):
        handle(event(), store, FakeLlm("", exc=ConnectionError()))
    assert store.get_trace("t1")["status"] == "received"


class FakeSdk:
    def __init__(self, stop_reason="end_turn", text="{}"):
        self.calls = []
        self.messages = self
        self._resp = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)],
                                     usage=SimpleNamespace(input_tokens=10, output_tokens=5), _request_id="req_1")

    def create(self, **kw):
        self.calls.append(kw)
        return self._resp


def test_anthropic_client_request_shape():
    sdk = FakeSdk(text='{"a": 1}')
    r = AnthropicClient(sdk, effort="low").complete(system="S", user=RedactedText.mint("U"), schema={"type": "object"},
                                                     model="claude-haiku-5-5", max_tokens=2048)
    call = sdk.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["max_tokens"] == 2048 and call["system"] == "S"
    assert call["messages"] == [{"role": "user", "content": "U"}]
    assert call["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "low"}
    assert "temperature" not in call and "tool_choice" not in call and "thinking" not in call
    assert r.text == '{"a": 1}' and r.request_id == "req_1" and (r.input_tokens, r.output_tokens) == (10, 5)


@pytest.mark.parametrize("stop,exc", [("refusal", LlmRefusal), ("max_tokens", LlmTruncated)])
def test_anthropic_client_maps_stop_reasons(stop, exc):
    with pytest.raises(exc):
        AnthropicClient(FakeSdk(stop_reason=stop)).complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)


@pytest.mark.parametrize("client", [AnthropicClient(FakeSdk()), StubLlm()])
def test_only_redacted_text_reaches_a_model(client):
    with pytest.raises(TypeError):
        client.complete(system="S", user="raw text", schema={}, model="m", max_tokens=1)


def test_prompt_files_are_read_fresh_and_versioned(tmp_path):
    d = tmp_path / "tier1"
    d.mkdir()
    (d / "system.md").write_text("S1")
    (d / "user.md").write_text("{message_time}|{sender}|{message}")
    (d / "schema.json").write_text('{"type": "object"}')
    v1 = interpret.load_prompt(directory=tmp_path)
    (d / "system.md").write_text("S2")
    v2 = interpret.load_prompt(directory=tmp_path)
    assert (v1.system, v2.system) == ("S1", "S2") and v1.version != v2.version


def test_message_text_with_braces_is_not_a_template_field():
    p = interpret.build_user_message(event("54 {no} crew {0}"), interpret.load_prompt())
    assert "54 {no} crew {0}" in p


def test_schema_file_matches_the_validation_models():
    props = interpret.load_prompt().schema["properties"]["commands"]["items"]["properties"]
    assert set(props) == set(interpret.Command.model_fields)
    assert set(props["action"]["enum"]) == set(interpret.Command.model_fields["action"].annotation.__args__)


def test_long_clarifying_question_is_traced_and_finishes(store):
    """Regression: a >64 char question was rejected by the trace guard and the message looped forever."""
    q = "Which squad is short a member tonight, and which hours do you need covered for the shift?"
    handle(event("We are short a member tonight"), store, FakeLlm(reply(commands=[cmd("Clarify", question=q)])))
    status, st = steps(store)
    assert status == "done" and st["reply"]["output"]["lines"] == [f"Ask sender: {q}"]
    assert st["interpret"]["output"]["commands"][0]["question"] == q


def test_unexpected_failure_after_the_model_call_is_final_not_a_loop(store, monkeypatch):
    llm = FakeLlm(reply(commands=[cmd("NoCrew", ["54"], "2025-03-05", "2200", "0100")]))
    monkeypatch.setattr(interpret.Command, "render", lambda self: 1 / 0)
    handle(event(), store, llm)  # does not raise
    assert steps(store)[0] == "error" and len(llm.calls) == 1


def test_non_transient_model_error_is_final(store):
    handle(event(), store, FakeLlm("", exc=RuntimeError("bad request")))
    assert steps(store)[0] == "error"


def test_redelivery_of_a_finished_trace_is_skipped(store):
    llm = FakeLlm(reply(is_scheduling=False))
    handle(event(), store, llm)
    handle(event(), store, llm)
    assert len(llm.calls) == 1 and [s["name"] for s in store.get_trace("t1")["steps"]].count("agent_received") == 1
