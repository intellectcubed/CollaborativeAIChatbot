"""Replay tool: impersonate a member at a chosen time and post to the message processor.

It talks to ingest over HTTP exactly as GroupMe would (plus X-Source: replay) and only reads the
trace store. Raw text is never written here; responses carry only trace ids and redacted trace data.
"""

import os
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from chatbot.config import GROUP_TZ, Settings, load_roster
from chatbot.tracing.store import TraceStore

PAGE = Path(__file__).with_name("page.html")


class SendRequest(BaseModel):
    sender_id: str
    text: str
    sent_at: datetime | None = None  # naive values are read as America/New_York; default is now


def create_app(settings=None, store=None, roster=None, post=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TraceStore(settings.trace_db_path)
    roster = roster or load_roster(settings.roster_path)
    ingest_url = os.environ.get("INGEST_URL", "http://localhost:8000")
    token = settings.webhook_token

    def default_post(payload: dict) -> dict:
        r = httpx.post(f"{ingest_url}/webhook", json=payload, headers={"X-Source": "replay"},
                       params={"token": token} if token else None, timeout=15)
        r.raise_for_status()
        return r.json()

    post = post or default_post
    app = FastAPI(title="chatbot replay")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/members")
    def members():
        # Real names are shown here so you can pick who to impersonate; this page is local only.
        # Traces and everything downstream use the Member-N pseudonym.
        return [{"sender_id": m.sender_id, "member": m.member, "squad": m.squad,
                 "name": m.names[0] if m.names else m.member} for m in roster.people()]

    @app.post("/api/send")
    def send(req: SendRequest):
        member = roster.lookup(req.sender_id)
        if member is None:
            raise HTTPException(404, "unknown sender")
        when = req.sent_at or datetime.now(GROUP_TZ)
        if when.tzinfo is None:
            when = when.replace(tzinfo=GROUP_TZ)
        payload = {
            "id": uuid.uuid4().hex,
            "created_at": int(when.timestamp()),
            "group_id": "replay",
            "sender_id": member.sender_id,
            "sender_type": "user",
            "name": member.names[0] if member.names else member.member,
            "text": req.text,
            "system": False,
            "attachments": [],
        }
        try:
            return post(payload)
        except httpx.HTTPError:
            raise HTTPException(502, "message processor unavailable")

    @app.get("/api/traces")
    def traces():
        return store.list_traces(limit=50)

    @app.get("/api/traces/{trace_id}")
    def trace(trace_id: str):
        t = store.get_trace(trace_id)
        if t is None:
            raise HTTPException(404)
        return t

    return app
