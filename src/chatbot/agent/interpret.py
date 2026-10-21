"""Tier 1 interpretation: one cheap-model call that turns a redacted message into calendar commands.

The commands are plain data for now. They are rendered as text ("Call NoCrew on calendar for date ...");
later they become calendar service calls.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator

from chatbot.config import GROUP_TZ, PROMPTS_DIR
from chatbot.models import Event, RedactedText

@dataclass(frozen=True)
class Prompt:
    system: str
    user_template: str
    schema: dict
    version: str  # short hash of the three files, recorded in the trace


def load_prompt(name: str = "tier1", directory: Path | None = None) -> Prompt:
    """Read the prompt files fresh, so edits apply to the next message without a restart."""
    d = (directory or PROMPTS_DIR) / name
    system = (d / "system.md").read_text(encoding="utf-8").strip()
    user_template = (d / "user.md").read_text(encoding="utf-8").rstrip("\n")
    schema_text = (d / "schema.json").read_text(encoding="utf-8")
    digest = hashlib.sha256("\0".join((system, user_template, schema_text)).encode()).hexdigest()[:8]
    return Prompt(system, user_template, json.loads(schema_text), digest)


class Command(BaseModel):
    action: Literal["NoCrew", "AddCrew", "GetShift", "Clarify"]
    squads: list[str]
    date: str
    start: str
    end: str
    question: str

    @field_validator("date")
    @classmethod
    def _date(cls, v):
        if v:
            date.fromisoformat(v)  # raises ValueError on garbage
        return v

    def trace_dict(self) -> dict:
        d = self.model_dump()
        d["question"] = RedactedText.mint(self.question)  # model output derived from redacted input; can be long
        return d

    def render(self) -> str:
        if self.action == "Clarify":
            return f"Ask sender: {self.question}"
        if self.action == "GetShift":
            return f"Get shift for date {self.date}"
        details = ", ".join(x for x in (f"squad {','.join(self.squads)}" if self.squads else "", f"{self.start}-{self.end}" if self.start else "") if x)
        return f"Call {self.action} on calendar for date {self.date}" + (f" ({details})" if details else "")


class Interpretation(BaseModel):
    is_scheduling: bool
    confidence: float
    commands: list[Command]
    reasoning: str


def build_user_message(event: Event, prompt: Prompt) -> RedactedText:
    """The user message: when it was sent, who sent it (roster pseudonym and squad), and the redacted text."""
    sent = event.sent_at.astimezone(GROUP_TZ)  # the queue round trip leaves only a fixed offset
    who = f"{event.member} (squad {event.squad})" if event.member else "unknown sender"
    return RedactedText.mint(prompt.user_template.format(
        message_time=f"{sent.strftime('%A %Y-%m-%d %H:%M')} {sent.tzname()}", sender=who, message=event.text))


def parse(text: str) -> Interpretation:
    """Validate the model's JSON. Raises ValueError (including pydantic's) if it is unusable."""
    return Interpretation.model_validate(json.loads(text))
