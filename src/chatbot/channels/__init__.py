"""Where the agent's replies go. Input is always a GroupMe-shaped webhook call; output is chosen by
where the message came from (`Event.source`), via config/channels.toml.

Every reply is written to the trace store's outbox first (see agent/stages/respond.py), then handed to
the routed channel. A channel only decides how a reply is delivered; the outbox is the record.
"""

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chatbot.config import CONFIG_DIR
from chatbot.models import RedactedText

log = logging.getLogger("channels")

KINDS = ("clarify", "confirm", "blocked", "result", "flag")


@dataclass(frozen=True)
class OutboundMessage:
    trace_id: str
    group_id: str
    to_sender_id: str
    to_member: str | None  # roster pseudonym; the real name never leaves the roster
    kind: str  # one of KINDS
    text: RedactedText
    squad: str | None = None  # the squad the reply is for, so chat history can group it with that squad


@dataclass(frozen=True)
class SendResult:
    status: str  # sent | skipped | failed
    error: str = ""


class Channel(Protocol):
    name: str

    def send(self, msg: OutboundMessage) -> SendResult: ...


class Channels:
    """The channels that exist and which one each source uses."""

    def __init__(self, available: dict[str, Channel], routes: dict[str, str]):
        self.available, self.routes = available, routes

    def for_source(self, source: str) -> Channel:
        name = self.routes.get(source, "log")
        if name not in self.available:
            log.warning("route %s=%s is not a known channel: using log", source, name)
            name = "log"
        return self.available[name]


DEFAULT_ROUTES = {"live": "log", "replay": "replay"}


def load_channels(path: Path | None = None) -> Channels:
    """Read config/channels.toml for every message. Anything missing or unusable falls back to
    safe routes: live replies are only logged, replay replies go to the outbox."""
    from chatbot.channels.builtin import LogChannel, ReplayChannel

    available = {c.name: c for c in (LogChannel(), ReplayChannel())}
    routes = dict(DEFAULT_ROUTES)
    path = Path(path or os.environ.get("CHANNELS_PATH") or CONFIG_DIR / "channels.toml")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("routes", {})
        routes.update({str(k): str(v) for k, v in data.items()})
    except FileNotFoundError:
        pass
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        log.warning("channels file unusable (%s): using safe defaults", type(exc).__name__)
    return Channels(available, routes)
