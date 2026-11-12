import json

from chatbot.interfaces.llm import LlmResult


def reply(**kw):
    base = {"is_scheduling": True, "confidence": 0.95, "commands": [], "reasoning": "test"}
    return json.dumps({**base, **kw})


def cmd(action, squads=(), date="", start="", end="", question=""):
    return {"action": action, "squads": list(squads), "date": date, "start": start, "end": end, "question": question}


NO_CREW_JSON = reply(commands=[cmd("NoCrew", ["54"], "2025-03-05", "2200", "0100")])


class FakeLlm:
    """Returns canned text and remembers what it was asked."""

    def __init__(self, text, exc=None):
        """`text` is one answer for every call, or a list of answers used in order."""
        self.text, self.exc, self.calls = text, exc, []

    def complete(self, *, system, user, schema, model, max_tokens, effort=None, extra=None):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model, "max_tokens": max_tokens,
                           "effort": effort, "extra": extra})
        if self.exc:
            raise self.exc
        text = self.text.pop(0) if isinstance(self.text, list) else self.text
        return LlmResult(text, model, 1000, 100, "end_turn", "req_test")


def make_manager(tmp_path, client, intent=True):
    """An LLMManager over one provider, with Haiku for both components, as the agent starts with."""
    from chatbot.interfaces.llm_config import from_models
    from chatbot.interfaces.llm_manager import LLMManager
    from chatbot.tracing.store import TraceStore

    model = "claude-haiku-5-5"
    config = from_models(model, model if intent else None)
    return LLMManager({"anthropic": client}, TraceStore(tmp_path / "manager.db"), config)


def intent_reply(is_schedule_change, confidence=0.95, reason="test", dates=(), direction="unclear"):
    """`dates` are the date words the model reports, e.g. [{"weekday": "friday"}]; missing fields are blank."""
    blank = {"weekday": "", "relative": "", "qualifier": "", "month": 0, "day": 0}
    return json.dumps({"is_schedule_change": is_schedule_change, "confidence": confidence, "reason": reason,
                       "date_references": [{**blank, **d} for d in dates], "direction": direction})
