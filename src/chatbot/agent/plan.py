"""What the agent intends to do to the calendar, one entry per squad per command."""

from dataclasses import dataclass
from datetime import date

from chatbot.models import RedactedText
from chatbot.schedule.verify import Verdict

LABELS = {"remove": "NoCrew", "add": "AddShift"}


@dataclass
class PlannedAction:
    kind: str  # remove | add | clarify | other
    squad: int | None = None
    shift_date: date | None = None
    start: str = ""
    end: str = ""
    verdict: Verdict | None = None
    # planned | noop | clarify | blocked | held | flagged | previewed | applied | failed | rolled_back
    status: str = "planned"
    reason: str = ""
    line: str = ""  # what to tell the sender / record
    change_id: str | None = None

    def summary(self) -> str:
        return f"{LABELS.get(self.kind, self.kind)} squad {self.squad}, {self.shift_date} {self.start}-{self.end}"

    def trace(self) -> dict:
        """Short fields only; the human line is marked redacted because it is built from redacted input."""
        return {"kind": self.kind, "squad": self.squad, "date": self.shift_date.isoformat() if self.shift_date else None,
                "start": self.start, "end": self.end, "status": self.status, "reason": self.reason,
                "partial": bool(self.verdict and self.verdict.partial), "change_id": self.change_id,
                "line": RedactedText.mint(self.line)}


def shift_hint(shift_date: date) -> str:
    if shift_date.weekday() >= 5:
        return "0600-1800 (day) and 1800-0600 (night)"
    return "1800-0600"
