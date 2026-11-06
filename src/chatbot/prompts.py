"""Prompt files: everything a model is told lives in prompts/<name>/, not in code."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from chatbot.config import PROMPTS_DIR


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
