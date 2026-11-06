"""A message goes through a list of stages. Each stage is a small class; the runner owns tracing,
error handling and early exit, so stages contain only their own logic.

Stage contract:
  - `run(ctx, step)` reads what earlier stages left on the context, writes its own result to the
    context, and describes what it did in `step.input` / `step.output` (the runner records the step).
  - To end the pipeline early, set `ctx.outcome` ("ignored", "error", "done", or "duplicate").
  - `applies(ctx)` can skip the stage. `fail_open` lets the message continue if the stage fails.
  - Raise only for failures worth retrying later (transient model errors): the runner re-raises them
    so the queue redelivers. Any other exception ends the trace as "error", or is skipped if fail_open.
"""

import logging
from dataclasses import dataclass, field

from chatbot.agent.dates import Resolution
from chatbot.agent.interpret import Interpretation
from chatbot.agent.intent import IntentResult
from chatbot.agent.plan import PlannedAction
from chatbot.agent.schedule_info import ScheduleInfo
from chatbot.channels import Channels
from chatbot.interfaces.llm import is_transient as _llm_transient
from chatbot.interfaces.traced_llm import BoundLlm
from chatbot.models import Event
from chatbot.policy import Policy
from chatbot.schedule.client import Calendar, CalendarUnavailable
from chatbot.tracing.store import Step, TraceStore

log = logging.getLogger("agent")

FINAL = {"done", "ignored", "error", "flagged"}


def is_transient(exc: Exception) -> bool:
    """Worth retrying later: a model or calendar outage, not a bad answer."""
    return isinstance(exc, CalendarUnavailable) or _llm_transient(exc)


@dataclass(frozen=True)
class AgentConfig:
    model: str = "claude-haiku-5-5"
    intent_model: str | None = None  # None switches the intent check off
    intent_skip_confidence: float = 0.8
    max_tokens: int = 2048
    intent_max_tokens: int = 1024


@dataclass
class Context:
    """Everything known about one message while it moves through the stages."""

    event: Event
    store: TraceStore
    llm: BoundLlm
    config: AgentConfig
    intent: IntentResult | None = None
    dates: Resolution | None = None  # dates worked out from the intent step's date words; None: not extracted
    schedule: ScheduleInfo | None = None  # the calendar on those dates; None: not looked up
    short_circuit: bool = False  # the schedule lookup already has a question for the sender: skip Tier 1
    interpretation: Interpretation | None = None
    lines: list[str] = field(default_factory=list)
    calendar: Calendar | None = None  # None: the calendar stages are skipped
    day_cache: dict = field(default_factory=dict)  # date -> DaySchedule, so each day is fetched once per message
    policy: Policy = field(default_factory=Policy)
    plan: list[PlannedAction] = field(default_factory=list)
    decision: str | None = None  # from the gate: apply | confirm | flag
    channels: Channels | None = None  # where replies go; None: nothing is sent
    outcome: str | None = None


class Stage:
    name = "stage"
    traced = True  # False: no trace step (used by the duplicate check)
    fail_open = False

    def applies(self, ctx: Context) -> bool:
        return True

    def run(self, ctx: Context, step: Step) -> None:
        raise NotImplementedError


def run_pipeline(stages: list[Stage], ctx: Context) -> None:
    tid = ctx.event.trace_id
    for stage in stages:
        if ctx.outcome is not None:
            break
        if not stage.applies(ctx):
            continue
        try:
            if stage.traced:
                with ctx.store.step(tid, stage.name) as step:
                    stage.run(ctx, step)
            else:
                stage.run(ctx, Step())
        except Exception as exc:
            if is_transient(exc):
                raise
            log.warning("trace=%s stage %s failed: %s", tid, stage.name, type(exc).__name__)
            if stage.fail_open:
                continue
            ctx.outcome = "error"
            break
    if ctx.outcome != "duplicate":
        ctx.store.set_status(tid, ctx.outcome or "done")
