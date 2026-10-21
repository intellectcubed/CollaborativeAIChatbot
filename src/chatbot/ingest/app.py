"""Webhook target: validate, filter PII, trace, enqueue.

The same route serves live GroupMe callbacks and the replay tool (header X-Source: replay).
Raw message text lives only in this function's memory. Nothing logs it, and the validation error
handler does not echo request bodies.
"""

import logging
import secrets

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from chatbot.config import Settings, load_allowlist, load_roster
from chatbot.models import Event, GroupMeCallback
from chatbot.pii.filter import PiiFilter
from chatbot.sqs import SqsPublisher, make_client
from chatbot.tracing.store import TraceStore

log = logging.getLogger("ingest")
SOURCES = {"live", "replay"}


def create_app(settings=None, store=None, publisher=None, roster=None, pii=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TraceStore(settings.trace_db_path)
    roster = roster or load_roster(settings.roster_path)
    pii = pii or PiiFilter(load_allowlist(), roster.name_tokens())
    publisher = publisher or SqsPublisher(make_client(settings), settings.queue_url)

    app = FastAPI(title="chatbot ingest")

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        # The default handler echoes the offending input, which could be message text.
        return JSONResponse({"status": "invalid", "fields": [".".join(map(str, e["loc"])) for e in exc.errors()]}, status_code=422)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post("/webhook")
    def webhook(payload: GroupMeCallback, token: str | None = Query(None), x_source: str = Header("live")):
        if settings.webhook_token and not (token and secrets.compare_digest(token, settings.webhook_token)):
            raise HTTPException(status_code=401)
        if x_source not in SOURCES:
            raise HTTPException(status_code=422, detail="X-Source must be live or replay")

        trace_id = payload.id
        member = roster.lookup(payload.sender_id)
        if not store.start_trace(trace_id, x_source, payload.sender_id, payload.sent_at.isoformat()):
            return {"status": "duplicate", "trace_id": trace_id}

        with store.step(trace_id, "inbound") as s:
            s.output = {
                "source": x_source,
                "sender_type": payload.sender_type,
                "member": member.member if member else None,
                "text_length": len(payload.text or ""),
                "attachments": len(payload.attachments),
            }
        if payload.system or payload.sender_type == "bot":
            store.set_status(trace_id, "ignored")
            return {"status": "ignored", "trace_id": trace_id}

        with store.step(trace_id, "pii_filter") as s:
            result = pii.apply(payload.text, payload.name)
            s.output = {
                "outcome": result.outcome.value,
                "reason": result.reason,
                "redactions": result.redactions,
                "text": result.text,
            }
        if not result.allowed:
            store.set_status(trace_id, f"filtered_{result.outcome.value}")
            log.info("trace=%s outcome=%s reason=%s", trace_id, result.outcome.value, result.reason)
            return {"status": result.outcome.value, "trace_id": trace_id}

        event = Event(
            trace_id=trace_id,
            source=x_source,
            group_id=payload.group_id,
            sender_id=payload.sender_id,
            member=member.member if member else None,
            squad=member.squad if member else None,
            sent_at=payload.sent_at,
            text=result.text,
            pii_outcome=result.outcome.value,
            pii_redactions=result.redactions,
        )
        try:
            with store.step(trace_id, "enqueue") as s:
                s.output = {"message_id": publisher.publish(event)}
        except Exception:
            store.set_status(trace_id, "enqueue_failed")
            log.exception("trace=%s enqueue failed", trace_id)
            raise HTTPException(status_code=503, detail="queue unavailable")
        store.set_status(trace_id, "enqueued")
        log.info("trace=%s enqueued", trace_id)
        return {"status": "enqueued", "trace_id": trace_id}

    return app
