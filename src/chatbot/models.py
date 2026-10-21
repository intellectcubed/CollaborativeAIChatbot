"""Wire and internal models.

`RedactedText` marks text that has passed the PII filter. The trace store and the queue only
accept message content in this type, so raw chat text cannot be persisted by accident.
"""

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler
from pydantic_core import core_schema

from chatbot.config import GROUP_TZ


class RedactedText(str):
    """Text that is safe to persist, queue, log and send to a model.

    Mint one with `RedactedText.mint`. A test restricts which modules may call it.
    """

    __slots__ = ()

    @classmethod
    def mint(cls, value: str) -> "RedactedText":
        return cls(value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler: GetCoreSchemaHandler):
        # Python values must already be RedactedText (a plain str is rejected). JSON read back
        # from the queue or trace store is trusted: only redacted text is ever written there.
        return core_schema.json_or_python_schema(
            json_schema=core_schema.no_info_after_validator_function(cls.mint, core_schema.str_schema()),
            python_schema=core_schema.is_instance_schema(cls),
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )


class GroupMeCallback(BaseModel):
    """The JSON GroupMe POSTs to a bot callback URL (and what the replay tool imitates)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    created_at: int  # unix seconds, UTC
    group_id: str = ""
    sender_id: str = ""
    sender_type: str = "user"
    name: str = ""
    text: str | None = None
    system: bool = False
    attachments: list[dict] = Field(default_factory=list)

    @property
    def sent_at(self) -> datetime:
        return datetime.fromtimestamp(self.created_at, timezone.utc).astimezone(GROUP_TZ)


class Event(BaseModel):
    """What goes on the queue. Contains redacted text only."""

    trace_id: str
    source: str  # "live" or "replay"
    group_id: str
    sender_id: str
    member: str | None  # roster pseudonym, None for an unknown sender
    squad: str | None
    sent_at: datetime  # timezone-aware, America/New_York
    text: RedactedText
    pii_outcome: str
    pii_redactions: dict[str, int] = Field(default_factory=dict)
