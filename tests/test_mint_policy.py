"""RedactedText.mint may only be called from modules that are allowed to vouch for redaction."""

from pathlib import Path

ALLOWED = {"models.py", "pii/filter.py", "agent/worker.py", "agent/interpret.py"}  # models: queue JSON read-back; worker: replies the agent composes itself
SRC = Path(__file__).resolve().parents[1] / "src" / "chatbot"


def test_mint_is_confined():
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "RedactedText.mint(" in p.read_text() and str(p.relative_to(SRC)) not in ALLOWED
    ]
    assert offenders == []
