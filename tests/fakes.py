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
        self.text, self.exc, self.calls = text, exc, []

    def complete(self, *, system, user, schema, model, max_tokens):
        self.calls.append({"system": system, "user": user, "schema": schema, "model": model, "max_tokens": max_tokens})
        if self.exc:
            raise self.exc
        return LlmResult(self.text, model, 1000, 100, "end_turn", "req_test")
