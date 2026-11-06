"""Tier 1 interpretation: one cheap-model call that turns a redacted message into calendar commands.

The commands are plain data for now. They are rendered as text ("Call NoCrew on calendar for date ...");
later they become calendar service calls.
"""

import json
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from chatbot.agent.schedule_info import NO_SCHEDULE
from chatbot.config import GROUP_TZ
from chatbot.models import Event, RedactedText
from chatbot.prompts import Prompt, load_prompt  # noqa: F401  (re-exported for callers and tests)

NO_DATES = "not extracted"  # what the Tier 1 message says when the intent step did not report dates


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


def render_user(template: str, member: str | None, squad: str | None, sent_at: datetime, text: str,
                dates: str = NO_DATES, schedule: str = NO_SCHEDULE) -> str:
    """Fill a user template: when it was sent, who sent it (roster pseudonym and squad), and the text."""
    sent = sent_at.astimezone(GROUP_TZ)  # the queue round trip leaves only a fixed offset
    who = f"{member} (squad {squad})" if member else "unknown sender"
    return template.format(message_time=f"{sent.strftime('%A %Y-%m-%d %H:%M')} {sent.tzname()}", sender=who, message=text,
                           dates=dates, schedule=schedule)


def build_user_message(event: Event, prompt: Prompt, dates: str = NO_DATES, schedule: str = NO_SCHEDULE) -> RedactedText:
    return RedactedText.mint(render_user(prompt.user_template, event.member, event.squad, event.sent_at, event.text, dates, schedule))


def parse(text: str) -> Interpretation:
    """Validate the model's JSON. Raises ValueError (including pydantic's) if it is unusable."""
    return Interpretation.model_validate(json.loads(text))
