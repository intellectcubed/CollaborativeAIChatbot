import logging

from chatbot.agent.pipeline import Stage, is_transient

log = logging.getLogger("agent")


class Apply(Stage):
    """Make the planned changes, all or none.

    Dry run (the default): asks the calendar service to PREVIEW each change; nothing is written.
    Live (policy mode live AND CALENDAR_ALLOW_WRITES=1): applies each change and records its change id.
    If one fails, the ones already applied are rolled back in reverse order and the trace ends in error.
    """

    name = "apply"

    def applies(self, ctx):
        return ctx.calendar is not None and any(a.status == "planned" for a in ctx.plan)

    def run(self, ctx, step):
        # Both the policy (with CALENDAR_ALLOW_WRITES) and this message's own live-calendar setting must allow writes.
        live = ctx.policy.live and ctx.event.live_calendar
        applied = []
        failure = None
        for a in [a for a in ctx.plan if a.status == "planned"]:
            call = ctx.calendar.no_crew if a.kind == "remove" else ctx.calendar.add_shift
            try:
                result = call(a.shift_date, a.start, a.end, a.squad, preview=not live)
            except Exception as exc:
                if not applied and is_transient(exc):
                    raise  # nothing changed yet: let the queue retry
                a.status, a.reason, failure = "failed", type(exc).__name__, exc
                break
            a.change_id = result.change_id
            a.status = "applied" if live else "previewed"
            a.line = (f"Called {a.summary()} (change {a.change_id})" if live else f"Would call {a.summary()}"
                      + (" (only part of that time was scheduled)" if a.verdict and a.verdict.partial else ""))
            if live:
                applied.append(a)
        if failure is not None:
            self._roll_back(ctx, applied)
            ctx.outcome = "error"
        step.output = {"mode": "live" if live else "dry_run", "live_calendar": ctx.event.live_calendar,
                       "actions": [a.trace() for a in ctx.plan],
                       "error": type(failure).__name__ if failure else None}

    @staticmethod
    def _roll_back(ctx, applied):
        for a in reversed(applied):
            try:
                ctx.calendar.rollback(a.shift_date, a.change_id)
                a.status = "rolled_back"
            except Exception as exc:
                log.error("trace=%s rollback of %s failed: %s", ctx.event.trace_id, a.change_id, type(exc).__name__)
