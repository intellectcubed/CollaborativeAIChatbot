import json
import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from chatbot.agent.worker import handle
from chatbot.interfaces.llm import LlmError, LlmRefusal, LlmResult, LlmTruncated, ProviderError, StubLlm
from chatbot.interfaces.llm_config import (ComponentConfig, ConfigError, LlmConfig, ModelSettings, from_models, load, parse)
from chatbot.interfaces.llm_manager import BudgetExceeded, LLMManager
from chatbot.interfaces.openrouter import OpenRouterClient
from chatbot.models import Event, RedactedText
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, intent_reply, reply, cmd

REPO = Path(__file__).resolve().parents[1]
HAIKU = "claude-haiku-5-5"


def tier1_block(extra=None):
    return {"source": "anthropic", "model": HAIKU, "max_tokens": 2048, **(extra or {})}


# ---- config file

def test_the_committed_config_loads_and_names_the_components():
    cfg = load(REPO / "config" / "llm.toml")
    assert set(cfg.components) == {"intent", "tier1"}
    assert cfg.components["tier1"].settings.model == HAIKU and cfg.components["tier1"].escalate is None
    assert cfg.budget.kill_switch is False and cfg.budget.per_message_usd > 0


def test_a_full_component_parses_including_escalation_and_extra():
    cfg = parse({"components": {"tier1": {**tier1_block({"effort": "high"}),
                                          "escalate": {"source": "openrouter", "model": "openai/gpt-4o", "max_tokens": 100,
                                                       "extra": {"temperature": 0.2}}}},
                 "budget": {"per_message_usd": 0.1, "per_day_usd": 2, "kill_switch": True}})
    comp = cfg.components["tier1"]
    assert comp.settings.effort == "high"
    assert comp.escalate == ModelSettings("openrouter", "openai/gpt-4o", 100, None, {"temperature": 0.2})
    assert cfg.budget.kill_switch is True and cfg.budget.per_day_usd == 2.0


@pytest.mark.parametrize("data,message", [
    ({"components": {"reply": tier1_block()}}, "unknown component"),
    ({"components": {"tier1": tier1_block({"source": "openai"})}}, "source must be"),
    ({"components": {"tier1": {"source": "anthropic", "max_tokens": 10}}}, "model is required"),
    ({"components": {"tier1": tier1_block({"max_tokens": 0})}}, "max_tokens"),
    ({"components": {"tier1": tier1_block({"max_tokens": True})}}, "max_tokens"),
    ({"components": {"tier1": tier1_block({"effort": "extreme"})}}, "effort for anthropic must be"),
    ({"components": {"tier1": tier1_block({"extra": {"temperature": 1}})}}, "extra is only for openrouter"),
    ({"components": {"tier1": tier1_block({"source": "openrouter", "extra": {"model": "x"}})}}, "extra cannot set model"),
    ({"components": {"tier1": tier1_block({"source": "openrouter", "extra": {"messages": []}})}}, "extra cannot set messages"),
    ({"budget": {"per_day_usd": -1}}, "per_day_usd"),
    ({"budget": {"kill_switch": "yes"}}, "kill_switch"),
    ({"surprise": {}}, "unknown top-level section"),
])
def test_bad_config_is_refused_with_a_reason(data, message):
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_a_file_that_is_not_toml_is_refused(tmp_path):
    p = tmp_path / "llm.toml"
    p.write_text("[components.tier1\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(p)


# ---- manager

@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "m.db")
    s.start_trace("t1", "replay", "u", "x")
    return s


def prompt():
    from chatbot.prompts import Prompt
    return Prompt("SYS", "{message}", {"type": "object"}, "v1")


def ask(manager, component="tier1", escalate=False, trace="t1"):
    return manager.complete(component, prompt(), RedactedText.mint("hello"), trace_id=trace, escalate=escalate)


def steps(store, trace="t1"):
    return {s["name"]: s for s in store.get_trace(trace)["steps"]}


def test_a_call_records_model_effort_tokens_cost_and_timing(store):
    fake = FakeLlm('{"ok": true}')
    manager = LLMManager({"anthropic": fake}, store, from_models(HAIKU, HAIKU, effort="high"))
    ask(manager)
    s = steps(store)["llm:tier1"]
    assert s["input"]["source"] == "anthropic" and s["input"]["model"] == HAIKU and s["input"]["effort"] == "high"
    assert s["input"]["prompt_version"] == "v1" and s["input"]["extra_keys"] == []
    assert s["output"]["input_tokens"] == 1000 and s["output"]["output_tokens"] == 100
    assert s["output"]["cost_usd"] == 0.00015 and s["output"]["latency_ms"] >= 0
    assert s["output"]["cache_read_tokens"] == 0 and s["output"]["cache_write_tokens"] == 0
    assert fake.calls[0]["effort"] == "high"


def test_an_unknown_component_is_refused(store):
    manager = LLMManager({"anthropic": FakeLlm("{}")}, store, from_models(HAIKU, None))
    with pytest.raises(ConfigError, match="no config for component 'reply'"):
        manager.complete("reply", prompt(), RedactedText.mint("x"), trace_id="t1")


def test_a_config_that_names_an_unavailable_provider_stops_startup(store):
    cfg = LlmConfig({"tier1": ComponentConfig(ModelSettings("openrouter", "openai/gpt-4o", 100))})
    with pytest.raises(ConfigError, match="openrouter, which is not available"):
        LLMManager({"anthropic": FakeLlm("{}")}, store, cfg)


def test_a_saved_change_applies_to_the_next_call_and_a_bad_one_keeps_the_last_good(store, tmp_path):
    path = tmp_path / "llm.toml"
    path.write_text('[components.tier1]\nmodel = "claude-haiku-5-5"\nmax_tokens = 100\n')
    manager = LLMManager({"anthropic": FakeLlm("{}")}, store, path)
    assert manager.config().components["tier1"].settings.max_tokens == 100
    path.write_text('[components.tier1]\nmodel = "claude-haiku-5-5"\nmax_tokens = 500\n')
    os.utime(path, (2, 2))  # explicit modification times, so the change is seen even on coarse file clocks
    assert manager.config().components["tier1"].settings.max_tokens == 500
    path.write_text('[components.tier1]\nmodel = "claude-haiku-5-5"\nmax_tokens = 0\n')
    os.utime(path, (3, 3))
    assert manager.config().components["tier1"].settings.max_tokens == 500  # last good kept


def test_a_per_message_cap_refuses_the_next_call_and_the_trace_says_why(store):
    manager = LLMManager({"anthropic": FakeLlm(["{}", "{}"])}, store,
                         LlmConfig({"tier1": ComponentConfig(ModelSettings("anthropic", HAIKU, 100))}, budget=_budget(per_message=0.0001)))
    ask(manager)  # spends 0.00015: the cap is checked before each call, so this one goes ahead
    with pytest.raises(BudgetExceeded, match="this message has spent"):
        ask(manager, escalate=False)
    assert steps(store)["llm:tier1"]["error"] == "BudgetExceeded"  # the refused call is recorded


def test_the_daily_cap_counts_every_message(store):
    manager = LLMManager({"anthropic": FakeLlm(["{}", "{}"])}, store,
                         LlmConfig({"tier1": ComponentConfig(ModelSettings("anthropic", HAIKU, 100))}, budget=_budget(per_day=0.0001)))
    ask(manager)
    store.start_trace("t2", "replay", "u", "x")
    with pytest.raises(BudgetExceeded, match="daily cap"):
        ask(manager, trace="t2")


def test_the_kill_switch_refuses_every_call_without_calling_the_provider(store):
    fake = FakeLlm("{}")
    manager = LLMManager({"anthropic": fake}, store,
                         LlmConfig({"tier1": ComponentConfig(ModelSettings("anthropic", HAIKU, 100))}, budget=_budget(kill=True)))
    with pytest.raises(BudgetExceeded, match="kill switch"):
        ask(manager)
    assert fake.calls == []


def test_a_bare_client_is_wrapped_in_the_manager_so_its_calls_are_recorded(store):
    fake = FakeLlm(intent_reply(False, 0.97))
    handle(event(), store, fake, intent_model=HAIKU)
    assert "llm:intent" in steps(store) and fake.calls[0]["model"] == HAIKU


def test_cache_counters_are_priced(monkeypatch):
    r = LlmResult("{}", HAIKU, input_tokens=100, output_tokens=0, cache_read_tokens=1000, cache_write_tokens=1000)
    # 100 + 1000 x 1.25 + 1000 x 0.1 input tokens at $0.10 per million
    assert r.cost_usd == round((100 + 1250 + 100) * 0.10 / 1_000_000, 6)


def test_a_reported_cost_is_used_as_is():
    r = LlmResult("{}", "openai/gpt-4o", 10, 10, reported_cost_usd=0.0042)
    assert r.cost_usd == 0.0042


def _budget(per_message=0.05, per_day=5.0, kill=False):
    from chatbot.interfaces.llm_config import Budget
    return Budget(per_message_usd=per_message, per_day_usd=per_day, kill_switch=kill)


def event():
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-2", squad="54",
                 sent_at=datetime.fromisoformat("2025-03-05T14:10:00-05:00"), text=RedactedText.mint("54 short"), pii_outcome="pass")


# ---- escalation

def escalating(store, tier1_escalate=True, fake=None):
    settings = ModelSettings("anthropic", HAIKU, 2048, "low")
    comp = ComponentConfig(settings, ModelSettings("anthropic", "claude-sonnet-5-5", 2048, "high") if tier1_escalate else None)
    fake = fake or FakeLlm([intent_reply(True, 0.9), reply(commands=[cmd("NoCrew", ["54"], "2025-03-05", "2200", "0100")], confidence=0.7),
                            reply(commands=[cmd("NoCrew", ["54"], "2025-03-05", "2200", "0100")], confidence=0.95)])
    manager = LLMManager({"anthropic": fake}, store, LlmConfig({"intent": ComponentConfig(settings), "tier1": comp}))
    return manager, fake


def test_a_low_confidence_answer_is_escalated_once_and_the_trace_shows_both(store):
    manager, fake = escalating(store)
    handle(event(), store, manager)
    st = steps(store)
    assert st["interpret"]["output"]["escalated"] is True and st["interpret"]["output"]["first_confidence"] == 0.7
    assert st["llm:tier1+escalate"]["input"]["model"] == "claude-sonnet-5-5"
    assert fake.calls[-1]["model"] == "claude-sonnet-5-5" and len(fake.calls) == 3


def test_the_next_message_starts_again_from_the_configured_model(store):
    fake = FakeLlm([intent_reply(True, 0.9), reply(commands=[], confidence=0.7), reply(commands=[], confidence=0.7),
                    intent_reply(True, 0.9), reply(commands=[], confidence=0.7), reply(commands=[], confidence=0.7)])
    manager, fake = escalating(store, fake=fake)
    handle(event(), store, manager)
    store.start_trace("t2", "replay", "u", "x")
    handle(event().model_copy(update={"trace_id": "t2"}), store, manager)
    tier1_models = [c["model"] for c in fake.calls if c["schema"].get("required") == ["is_scheduling", "confidence", "commands", "reasoning"]]
    assert tier1_models == [HAIKU, "claude-sonnet-5-5", HAIKU, "claude-sonnet-5-5"]


@pytest.mark.parametrize("confidence,escalated", [(0.95, False), (0.4, False), (0.9, False), (0.5, True)])
def test_only_the_low_band_escalates(store, confidence, escalated):
    manager, fake = escalating(store, fake=FakeLlm([intent_reply(True, 0.9), reply(commands=[], confidence=confidence),
                                                    reply(commands=[], confidence=confidence)]))
    handle(event(), store, manager)
    assert steps(store)["interpret"]["output"]["escalated"] is escalated


def test_without_an_escalate_table_there_is_no_second_call(store):
    manager, fake = escalating(store, tier1_escalate=False, fake=FakeLlm([intent_reply(True, 0.9), reply(commands=[], confidence=0.7)]))
    handle(event(), store, manager)
    assert len(fake.calls) == 2


# ---- OpenRouter

def router(handler):
    return OpenRouterClient("sk-test", http=httpx.Client(transport=httpx.MockTransport(handler)))


def ok_body(content='{"is_schedule_change": false}', finish="stop", cost=0.0003):
    return {"id": "gen_1", "model": "openai/gpt-4o", "choices": [{"message": {"content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 9, "cost": cost}}


def test_openrouter_sends_the_schema_and_the_extra_parameters_and_reads_the_reported_cost():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json=ok_body())

    r = router(handler).complete(system="S", user=RedactedText.mint("U"), schema={"type": "object"}, model="openai/gpt-4o",
                                 max_tokens=200, extra={"temperature": 0.2})
    body = seen["body"]
    assert body["model"] == "openai/gpt-4o" and body["max_tokens"] == 200 and body["temperature"] == 0.2
    assert body["messages"] == [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert seen["auth"] == "Bearer sk-test"
    assert (r.input_tokens, r.output_tokens, r.cost_usd, r.request_id) == (120, 9, 0.0003, "gen_1")


def test_openrouter_maps_its_failures_to_transient_or_final():
    def status(code):
        return router(lambda request: httpx.Response(code, json={"error": {"message": "nope"}}))

    with pytest.raises(ProviderError):
        status(429).complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)
    with pytest.raises(ProviderError):
        status(503).complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)
    with pytest.raises(LlmError, match="400"):
        status(400).complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)
    def unreachable(request):
        raise httpx.ConnectError("down")

    with pytest.raises(ProviderError):
        router(unreachable).complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)


@pytest.mark.parametrize("finish,exc", [("length", LlmTruncated), ("content_filter", LlmRefusal)])
def test_openrouter_stop_reasons(finish, exc):
    with pytest.raises(exc):
        router(lambda request: httpx.Response(200, json=ok_body(finish=finish))).complete(
            system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)


def test_openrouter_only_takes_redacted_text():
    with pytest.raises(TypeError):
        router(lambda request: httpx.Response(200, json=ok_body())).complete(system="S", user="raw", schema={}, model="m", max_tokens=1)


# ---- the replay tool's settings panel

def test_the_writer_round_trips_the_committed_file_and_refuses_bad_data():
    from chatbot.interfaces.llm_config import dumps, to_dict
    import tomllib
    cfg = load(REPO / "config" / "llm.toml")
    assert parse(tomllib.loads(dumps(to_dict(cfg)))) == cfg
    assert parse(tomllib.loads(dumps({"components": {"tier1": {"source": "openrouter", "model": "a/b", "max_tokens": 5,
                                                               "extra": {"temperature": 0.2, "stop": ["x"], "flag": True}}}}))
                 ).components["tier1"].settings.extra == {"temperature": 0.2, "stop": ["x"], "flag": True}
    with pytest.raises(ConfigError):
        dumps({"components": {"tier1": {"model": object()}}})


def test_the_replay_panel_reads_and_saves_the_file_and_refuses_bad_saves(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from chatbot.config import Settings
    from chatbot.replay.app import create_app as create_replay
    path = tmp_path / "llm.toml"
    path.write_text((REPO / "config" / "llm.toml").read_text())
    monkeypatch.setenv("LLM_CONFIG_PATH", str(path))
    base = Settings.from_env()
    client = TestClient(create_replay(Settings(str(tmp_path / "r.db"), base.queue_url, None, base.roster_path, None),
                                      TraceStore(tmp_path / "r.db")))
    got = client.get("/api/llm-config").json()
    assert set(got["components"]) == {"intent", "tier1"}
    got["components"]["tier1"]["model"] = "claude-sonnet-5-5"
    assert client.put("/api/llm-config", json=got).status_code == 200
    assert load(path).components["tier1"].settings.model == "claude-sonnet-5-5"
    bad = {**got, "components": {"tier1": {**got["components"]["tier1"], "effort": "extreme"}}}
    assert client.put("/api/llm-config", json=bad).status_code == 422
    assert load(path).components["tier1"].settings.model == "claude-sonnet-5-5"  # the bad save changed nothing
    assert "llm" in client.get("/api/status").json()


def test_effort_choices_depend_on_the_source():
    parse({"components": {"tier1": {"source": "openrouter", "model": "x/y", "max_tokens": 5, "effort": "xhigh"}}})
    with pytest.raises(ConfigError, match="effort for anthropic"):
        parse({"components": {"tier1": {"source": "anthropic", "model": HAIKU, "max_tokens": 5, "effort": "xhigh"}}})


def test_openrouter_effort_becomes_the_reasoning_setting_unless_the_user_set_one():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=ok_body())

    client = router(handler)
    client.complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1, effort="high")
    client.complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1, effort="high",
                    extra={"reasoning": {"max_tokens": 500}})
    client.complete(system="S", user=RedactedText.mint("U"), schema={}, model="m", max_tokens=1)
    assert seen[0]["reasoning"] == {"effort": "high"}
    assert seen[1]["reasoning"] == {"max_tokens": 500}
    assert "reasoning" not in seen[2]
