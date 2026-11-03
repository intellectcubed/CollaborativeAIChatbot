"""Policy: how confident the agent must be to act, whether it may write, and who may change what.

Read from config/policy.toml for every message, so a change applies to the next one. A missing or
invalid file falls back to the safest settings (dry run).
"""

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from chatbot.config import CONFIG_DIR

log = logging.getLogger("policy")
MODES = {"dry_run", "live"}


@dataclass(frozen=True)
class Policy:
    mode: str = "dry_run"
    apply_min_confidence: float = 0.9
    ask_min_confidence: float = 0.5
    allow_cross_squad_edits: bool = False

    @property
    def live(self) -> bool:
        """Writes happen only when the file says live AND the environment explicitly allows writes."""
        return self.mode == "live" and os.environ.get("CALENDAR_ALLOW_WRITES") == "1"

    @property
    def effective_mode(self) -> str:
        return "live" if self.live else "dry_run"

    def decide(self, confidence: float) -> str:
        """'apply', 'confirm' (ask the sender first) or 'flag' (admin)."""
        if confidence >= self.apply_min_confidence:
            return "apply"
        return "confirm" if confidence >= self.ask_min_confidence else "flag"


def load_policy(path: Path | None = None) -> Policy:
    path = Path(path or os.environ.get("POLICY_PATH") or CONFIG_DIR / "policy.toml")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        policy = Policy(
            mode=str(data.get("mode", "dry_run")),
            apply_min_confidence=float(data.get("apply_min_confidence", 0.9)),
            ask_min_confidence=float(data.get("ask_min_confidence", 0.5)),
            allow_cross_squad_edits=bool(data.get("allow_cross_squad_edits", False)),
        )
        if policy.mode not in MODES or not 0 <= policy.ask_min_confidence <= policy.apply_min_confidence <= 1:
            raise ValueError("invalid policy values")
        return policy
    except FileNotFoundError:
        return Policy()
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        log.warning("policy file unusable (%s): using safe defaults", type(exc).__name__)
        return Policy()
