"""Intent gate: a cheap model call that asks whether a message looks like a schedule change.

Only a confident 'no' stops the pipeline. Missing a real change is worse than a wasted Tier 1 call,
so a 'no' below the confidence threshold, or any failure of the detector, lets the message through.
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from chatbot.agent.dates import DateReference


class IntentResult(BaseModel):
    is_schedule_change: bool
    confidence: float
    reason: str
    date_references: list[DateReference] = Field(default_factory=list)  # the date words in the message, not dates
    # Is the squad giving up coverage ("remove"), taking on coverage ("add"), or is it unclear? Only "remove"
    # lets the schedule lookup ask "that squad is not on the schedule then", since adds are normally unscheduled.
    direction: Literal["remove", "add", "unclear"] = "unclear"


def parse(text: str) -> IntentResult:
    return IntentResult.model_validate(json.loads(text))


def should_skip(result: IntentResult, min_confidence: float) -> bool:
    return (not result.is_schedule_change) and result.confidence >= min_confidence
