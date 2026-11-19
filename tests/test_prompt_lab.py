import shutil
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.agent import lab
from chatbot.config import Settings
from chatbot.interfaces.llm import StubLlm
from chatbot.prompts import load_prompt
from chatbot.replay.app import create_app as create_replay
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, intent_reply, make_manager

REAL_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"


@pytest.fixture
def prompts(tmp_path):
    shutil.copytree(REAL_PROMPTS, tmp_path / "prompts")
    return tmp_path / "prompts"


@pytest.fixture
def stack(tmp_path, prompts):
    base = Settings.from_env()
    s = Settings(str(tmp_path / "trace.db"), base.queue_url, None, base.roster_path, None)
    store = TraceStore(s.trace_db_path)
    store.start_trace("t1", "replay", "u", "x")
    with store.step("t1", "agent_received") as st:
        from chatbot.models import RedactedText
        st.output = {"member": "Member-1", "squad": "54", "sent_at": "2026-11-02T14:10:00-05:00", "text": RedactedText.mint("no crew tonight")}
    sent = []

    def lab_post(body):
        sent.append(body)
        return {"text": "{}", "model": body["model"]}

    return TestClient(create_replay(s, store, prompts_dir=prompts, lab_post=lab_post)), sent, prompts


BODY = {"system": "S", "user_template": "From: {sender}\nWhen: {message_time}\n{message}", "trace_id": "t1",
        "model": "claude-haiku-5-5", "max_tokens": 100}


def test_get_prompt_returns_the_saved_files(stack):
    client, _, prompts = stack
    p = client.get("/api/prompts/intent").json()
    assert p["system"] == load_prompt("intent", prompts).system and p["version"] == load_prompt("intent", prompts).version


def test_unknown_or_unsafe_prompt_names_are_rejected(stack):
    client, _, _ = stack
    assert client.get("/api/prompts/nope").status_code == 404
    assert client.get("/api/prompts/..%2Fconfig").status_code == 404
    assert client.put("/api/prompts/..", json=BODY).status_code in (404, 405)


def test_render_fills_the_template_from_the_trace(stack):
    client, _, _ = stack
    user = client.post("/api/prompts/intent/render", json=BODY).json()["user"]
    assert "From: Member-1 (squad 54)" in user and "Monday 2026-11-02 14:10 EST" in user and user.endswith("no crew tonight")


def test_try_sends_the_edited_prompt_with_the_saved_schema(stack):
    client, sent, prompts = stack
    assert client.post("/api/prompts/intent/try", json=BODY).status_code == 200
    [call] = sent
    assert call["system"] == "S" and call["user"].startswith("From: Member-1") and call["max_tokens"] == 100
    assert call["schema"] == load_prompt("intent", prompts).schema  # the schema is not editable here


def test_try_without_the_agent_lab_is_a_clear_502(stack, tmp_path, prompts):
    import httpx

    def down(body):
        raise httpx.ConnectError("x")

    base = Settings.from_env()
    s = Settings(str(tmp_path / "t2.db"), base.queue_url, None, base.roster_path, None)
    store = TraceStore(s.trace_db_path)
    store.start_trace("t1", "replay", "u", "x")
    with store.step("t1", "agent_received") as st:
        st.output = {"member": None, "squad": None, "sent_at": "2026-11-02T14:10:00-05:00", "text": "x"}
    r = TestClient(create_replay(s, store, prompts_dir=prompts, lab_post=down)).post("/api/prompts/intent/try", json=BODY)
    assert r.status_code == 502 and "PROMPT_LAB" in r.json()["detail"]


def test_save_writes_the_files_and_changes_the_version(stack):
    client, _, prompts = stack
    before = load_prompt("intent", prompts)
    r = client.put("/api/prompts/intent", json={"system": "New system", "user_template": "{message}"})
    after = load_prompt("intent", prompts)
    assert r.json()["version"] == after.version != before.version
    assert (after.system, after.user_template, after.schema) == ("New system", "{message}", before.schema)


@pytest.mark.parametrize("template", ["{unknown}", "{", "{0}", "  "])
def test_save_refuses_a_broken_template_and_changes_nothing(stack, template):
    client, _, prompts = stack
    before = load_prompt("intent", prompts).version
    assert client.put("/api/prompts/intent", json={"system": "x", "user_template": template}).status_code == 422
    assert load_prompt("intent", prompts).version == before


def test_render_with_a_bad_template_is_422(stack):
    client, _, _ = stack
    assert client.post("/api/prompts/intent/render", json={**BODY, "user_template": "{nope}"}).status_code == 422


def test_lab_endpoint_calls_the_model_client(tmp_path):
    llm = FakeLlm(intent_reply(False))
    manager = make_manager(tmp_path, llm)
    client = TestClient(lab.create_app(manager))
    r = client.post("/complete", json={"system": "s", "user": "u", "schema": {"type": "object"}, "model": "claude-haiku-5-5", "max_tokens": 50})
    assert r.status_code == 200 and r.json()["text"] and llm.calls[0]["user"] == "u"
    [lab_trace] = [t for t in manager.store.list_traces(include_lab=True) if t["source"] == "lab"]
    assert [s["name"] for s in manager.store.get_trace(lab_trace["trace_id"])["steps"]] == ["llm:lab"]


def test_lab_endpoint_limits_the_model_and_tokens(tmp_path):
    client = TestClient(lab.create_app(make_manager(tmp_path, StubLlm())))
    base = {"system": "s", "user": "u", "schema": {}, "max_tokens": 50}
    assert client.post("/complete", json={**base, "model": "some-expensive-model"}).status_code == 422
    assert client.post("/complete", json={**base, "model": "claude-haiku-5-5", "max_tokens": 10**6}).status_code == 422


def test_lab_is_off_unless_enabled(monkeypatch, tmp_path):
    monkeypatch.delenv("PROMPT_LAB", raising=False)
    assert lab.start_if_enabled(make_manager(tmp_path, StubLlm())) is False


# ---- AI-assisted refinement

REFINE = {"system": "OLD SYSTEM {{hints}}", "user": "From: Member-1\nMessage: whole shift", "response": '{"is_schedule_change": true}',
          "hints": "It should have said no", "model": "claude-haiku-5-5"}


def refine_stack(tmp_path, prompts, answer):
    base = Settings.from_env()
    s = Settings(str(tmp_path / "r.db"), base.queue_url, None, base.roster_path, None)
    sent = []

    def lab_post(body):
        sent.append(body)
        return answer

    return TestClient(create_replay(s, TraceStore(s.trace_db_path), prompts_dir=prompts, lab_post=lab_post)), sent


GOOD = {"text": '{"system_prompt": "NEW SYSTEM", "explanation": "because"}', "model": "claude-sonnet-5-5",
        "input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001, "latency_ms": 7}


def test_refine_fills_the_template_and_returns_the_suggestion(tmp_path, prompts):
    client, sent = refine_stack(tmp_path, prompts, GOOD)
    r = client.post("/api/prompts/intent/refine", json=REFINE)
    assert r.status_code == 200 and (r.json()["system_prompt"], r.json()["explanation"]) == ("NEW SYSTEM", "because")
    [call] = sent
    for part in ("intent", "claude-haiku-5-5", "From: Member-1", '{"is_schedule_change": true}', "It should have said no"):
        assert part in call["user"]
    assert "OLD SYSTEM {{hints}}" in call["user"]  # a value that looks like a field is not filled a second time
    assert "{{" not in call["user"].replace("OLD SYSTEM {{hints}}", "")
    assert call["model"] == "claude-sonnet-5-5" and call["schema"]["required"] == ["system_prompt", "explanation"]


def test_refine_without_hints_says_so(tmp_path, prompts):
    client, sent = refine_stack(tmp_path, prompts, GOOD)
    client.post("/api/prompts/intent/refine", json={**REFINE, "hints": "  "})
    assert "(no hints given)" in sent[0]["user"]


def test_refine_template_problems_are_clear(tmp_path, prompts):
    client, _ = refine_stack(tmp_path, prompts, GOOD)
    (prompts / "prompt_refinement.md").write_text("{{system_prompt}} {{nope}}")
    r = client.post("/api/prompts/intent/refine", json=REFINE)
    assert r.status_code == 422 and "nope" in r.json()["detail"]
    (prompts / "prompt_refinement.md").unlink()
    assert client.post("/api/prompts/intent/refine", json=REFINE).status_code == 404


def test_refine_passes_model_errors_through_and_rejects_unreadable_answers(tmp_path, prompts):
    client, _ = refine_stack(tmp_path, prompts, {"error": "RateLimitError: slow down"})
    assert client.post("/api/prompts/intent/refine", json=REFINE).json() == {"error": "RateLimitError: slow down"}
    client, _ = refine_stack(tmp_path, prompts, {"text": "not json", "model": "m"})
    assert client.post("/api/prompts/intent/refine", json=REFINE).status_code == 502


def test_render_uses_the_dates_the_intent_step_worked_out(tmp_path, prompts):
    from chatbot.models import RedactedText

    base = Settings.from_env()
    s = Settings(str(tmp_path / "d.db"), base.queue_url, None, base.roster_path, None)
    store = TraceStore(s.trace_db_path)
    for tid, with_intent in (("t1", False), ("t2", True)):
        store.start_trace(tid, "replay", "u", "x")
        with store.step(tid, "agent_received") as st:
            st.output = {"member": "Member-1", "squad": "54", "sent_at": "2026-11-02T14:10:00-05:00", "text": RedactedText.mint("54 short Wednesday")}
        if with_intent:
            with store.step(tid, "intent") as st:
                st.output = {"dates_text": RedactedText.mint('"wednesday" = Wednesday 2026-11-04')}
    client = TestClient(create_replay(s, store, prompts_dir=prompts))
    body = {**BODY, "user_template": "{message}\nDates: {dates}"}
    assert client.post("/api/prompts/tier1/render", json={**body, "trace_id": "t1"}).json()["user"].endswith("Dates: not extracted")
    assert client.post("/api/prompts/tier1/render", json={**body, "trace_id": "t2"}).json()["user"].endswith('Dates: "wednesday" = Wednesday 2026-11-04')


def test_templates_may_use_the_dates_field(stack):
    client, _, prompts = stack
    r = client.put("/api/prompts/tier1", json={"system": "S", "user_template": "{message}\nDates: {dates}"})
    assert r.status_code == 200 and "Dates: {dates}" in (prompts / "tier1" / "user.md").read_text()


def test_the_committed_template_only_uses_known_fields():
    import re
    text = (REAL_PROMPTS / "prompt_refinement.md").read_text()
    fields = set(re.findall(r"\{\{(\w+)\}\}", text))
    assert fields == {"prompt_name", "model", "system_prompt", "user_message", "response_schema", "response", "hints"}


def test_a_temporary_model_is_used_for_this_call_only(stack):
    client, sent, _ = stack
    body = {**BODY, "override": {"source": "openrouter", "model": "openai/gpt-4o", "effort": None}}
    assert client.post("/api/prompts/intent/try", json=body).status_code == 200
    assert sent[-1]["model"] == "openai/gpt-4o" and sent[-1]["source"] == "openrouter" and sent[-1]["extra"] == {}
    client.post("/api/prompts/intent/try", json=BODY)  # no override: the saved config decides
    assert sent[-1]["model"] == BODY["model"] and sent[-1]["source"] == "anthropic"


def test_a_temporary_model_must_name_a_known_source(stack):
    client, _, _ = stack
    body = {**BODY, "override": {"source": "openai", "model": "x"}}
    assert client.post("/api/prompts/intent/try", json=body).status_code == 422


def test_the_agent_lists_anthropic_models_for_the_replay_tool(tmp_path):
    from types import SimpleNamespace
    from chatbot.interfaces.llm import AnthropicClient

    class Models:
        def list(self):
            return [SimpleNamespace(id="claude-b"), SimpleNamespace(id="claude-a")]

    sdk = SimpleNamespace(models=Models())
    client = TestClient(lab.create_app(make_manager(tmp_path, AnthropicClient(client=sdk))))
    assert client.get("/models/anthropic").json() == {"models": ["claude-a", "claude-b"]}
    stub = TestClient(lab.create_app(make_manager(tmp_path, StubLlm())))
    assert stub.get("/models/anthropic").status_code == 503
