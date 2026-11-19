"""Prompt lab: lets the replay tool try an edited prompt on the model client the agent already uses.

The replay container deliberately has no API key, so its Send button comes here. Off unless
PROMPT_LAB=1 on the agent, and only reachable on the compose network (the port is not published).
The text comes from a person editing a prompt in the replay tool, pre-filled from redacted trace data;
that person vouches for it, which is why this module may mint RedactedText.

Calls go through LLMManager like every other call, so budgets and cost apply. Each call is stored as its
own trace with source 'lab', which the replay tool keeps out of its trace list.
"""

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chatbot.interfaces.llm import PRICES
from chatbot.interfaces.llm_config import ModelSettings
from chatbot.interfaces.llm_manager import BudgetExceeded, LLMManager
from chatbot.models import RedactedText

log = logging.getLogger("lab")
MAX_TOKENS = 8192  # the most a prompt-lab call may ask for; the replay tool's Suggest box is limited to this


class TryRequest(BaseModel):
    system: str
    user: str
    schema_: dict = Field(alias="schema")
    source: str = "anthropic"
    model: str
    effort: str | None = None
    extra: dict = Field(default_factory=dict)
    max_tokens: int = Field(ge=1, le=MAX_TOKENS)


def create_app(manager: LLMManager) -> FastAPI:
    app = FastAPI(title="chatbot prompt lab")

    @app.get("/models/anthropic")
    def anthropic_models():
        """The models this account can call, for the replay tool's model list. Needs the agent's Anthropic key."""
        provider = manager.providers.get("anthropic")
        if not hasattr(provider, "list_models"):
            raise HTTPException(503, "no Anthropic key on the agent, so the model list is not available")
        try:
            return {"models": provider.list_models()}
        except Exception as exc:
            log.warning("model list failed: %s", type(exc).__name__)
            raise HTTPException(502, f"could not list models: {type(exc).__name__}")

    @app.post("/complete")
    def complete(req: TryRequest):
        if req.source == "anthropic" and req.model not in PRICES:
            raise HTTPException(422, f"model must be one of {sorted(PRICES)}")
        if req.source not in manager.providers:
            raise HTTPException(422, f"source {req.source} is not available")
        settings = ModelSettings(req.source, req.model, req.max_tokens, req.effort, req.extra)
        lab_id = uuid.uuid4().hex
        manager.store.start_trace(lab_id, "lab", "lab", datetime.now(timezone.utc).isoformat(), status="lab")
        t0 = time.monotonic()
        try:
            r = manager.call(settings, step_name="llm:lab", trace_id=lab_id, system=req.system,
                             user=RedactedText.mint(req.user), schema=req.schema_)
        except BudgetExceeded as exc:
            return {"error": f"refused by budget: {exc}"}
        except Exception as exc:
            log.warning("lab call failed: %s", type(exc).__name__)
            return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        manager.store.set_status(lab_id, "done")
        return {"text": r.text, "model": r.model, "effort": req.effort, "stop_reason": r.stop_reason,
                "input_tokens": r.input_tokens, "output_tokens": r.output_tokens, "cost_usd": r.cost_usd,
                "request_id": r.request_id, "latency_ms": int((time.monotonic() - t0) * 1000)}

    return app


def start_if_enabled(manager: LLMManager, port: int = 8002) -> bool:
    if os.environ.get("PROMPT_LAB") != "1":
        return False
    import uvicorn

    config = uvicorn.Config(create_app(manager), host="0.0.0.0", port=port, log_level="warning")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True, name="prompt-lab").start()
    log.info("prompt lab listening on :%s", port)
    return True
