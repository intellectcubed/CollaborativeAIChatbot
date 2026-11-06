"""RedactedText.mint may only be called from modules that are allowed to vouch for redaction."""

from pathlib import Path

# models: queue JSON read-back; interpret and the stages: text the agent composes from redacted input;
# traced_llm: records prompts that were built from redacted input;
# lab: prompt text a person edits in the replay tool (gated by PROMPT_LAB=1)
ALLOWED = {"models.py", "pii/filter.py", "agent/interpret.py", "agent/plan.py", "interfaces/traced_llm.py", "agent/lab.py"}  # and agent/stages/
SRC = Path(__file__).resolve().parents[1] / "src" / "chatbot"


def test_mint_is_confined():
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "RedactedText.mint(" in p.read_text()
        and str(p.relative_to(SRC)) not in ALLOWED
        and not str(p.relative_to(SRC)).startswith("agent/stages/")
    ]
    assert offenders == []
