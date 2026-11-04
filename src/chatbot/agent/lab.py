"""Prompt lab: lets the replay tool try an edited prompt on the model client the agent already uses.

The replay container deliberately has no API key, so its Send button comes here. Off unless
PROMPT_LAB=1 on the agent, and only reachable on the compose network (the port is not published).
The text comes from a person editing a prompt in the replay tool, pre-filled from redacted trace data;
that person vouches for it, which is why this module may mint RedactedText.
"""

import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from chatbot.interfaces.llm import PRICES, LlmClient
from chatbot.models import RedactedText

log = logging.getLogger("lab")
MAX_TOKENS = 4096


class TryRequest(BaseModel):
    system: str
    user: str
    schema_: dict = Field(alias="schema")
    model: str
    max_tokens: int = Field(ge=1, le=MAX_TOKENS)


def create_app(llm: LlmClient) -> FastAPI:
    app = FastAPI(title="chatbot prompt lab")

    @app.post("/complete")
    def complete(req: TryRequest):
        if req.model not in PRICES:
            raise HTTPException(422, f"model must be one of {sorted(PRICES)}")
        t0 = time.monotonic()
        try:
            r = llm.complete(system=req.system, user=RedactedText.mint(req.user), schema=req.schema_,
                             model=req.model, max_tokens=req.max_tokens)
        except Exception as exc:
            log.warning("lab call failed: %s", type(exc).__name__)
            return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        return {"text": r.text, "model": r.model, "stop_reason": r.stop_reason, "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens, "cost_usd": r.cost_usd, "request_id": r.request_id,
                "latency_ms": int((time.monotonic() - t0) * 1000)}

    return app


def start_if_enabled(llm: LlmClient, port: int = 8002) -> bool:
    if os.environ.get("PROMPT_LAB") != "1":
        return False
    import uvicorn

    config = uvicorn.Config(create_app(llm), host="0.0.0.0", port=port, log_level="warning")
    threading.Thread(target=uvicorn.Server(config).run, daemon=True, name="prompt-lab").start()
    log.info("prompt lab listening on :%s", port)
    return True
