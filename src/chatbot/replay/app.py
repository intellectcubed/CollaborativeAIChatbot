"""Replay tool: impersonate a member at a chosen time and post to the message processor.

It talks to ingest over HTTP exactly as GroupMe would (plus X-Source: replay) and only reads the
trace store. Raw text is never written here; responses carry only trace ids and redacted trace data.
The prompt editor is the one other thing it does: it saves prompts/<name>/{system,user}.md, and sends
trial calls through the agent (this container has no API key).
"""

import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from chatbot.channels import load_channels
from chatbot.agent.interpret import render_user
from chatbot.config import GROUP_TZ, PROMPTS_DIR, Settings, load_allowlist, load_roster
from chatbot.pii.filter import PiiFilter
from chatbot.policy import load_policy
from chatbot.replay import evaluate
from chatbot.prompts import load_prompt
from chatbot.tracing.store import TraceStore

PAGE = Path(__file__).with_name("page.html")
OUTBOX_PAGE = Path(__file__).with_name("outbox.html")


class SendRequest(BaseModel):
    sender_id: str
    text: str
    sent_at: datetime | None = None  # naive values are read as America/New_York; default is now


class PromptEdit(BaseModel):
    system: str
    user_template: str


class TryPrompt(PromptEdit):
    trace_id: str
    model: str
    max_tokens: int


class RefineRequest(BaseModel):
    system: str  # the system prompt as it is in the editor now
    user: str  # the user message that was sent
    response: str  # the response to fix: the latest one shown
    hints: str
    model: str  # the model the prompt runs on, so the suggestion is written for it


class SystemVariant(BaseModel):
    label: str
    system: str


class EvalRequest(BaseModel):
    user_template: str
    model: str
    max_tokens: int
    systems: list[SystemVariant]  # one prompt to score, or two to compare (e.g. current and suggested)


DEFAULT_CASES = Path(os.environ.get("TEST_CASES_PATH", Path(__file__).resolve().parents[3] / "test_cases" / "labeled_examples.csv"))
REFINE_TEMPLATE = "prompt_refinement.md"
REFINE_MODEL = os.environ.get("REFINE_MODEL", "claude-sonnet-5-5")
REFINE_SYSTEM = "Follow the instructions in the user message. Respond only with the JSON object they ask for."
REFINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["system_prompt", "explanation"],
                 "properties": {"system_prompt": {"type": "string"}, "explanation": {"type": "string"}}}


def create_app(settings=None, store=None, roster=None, post=None, prompts_dir=None, lab_post=None, cases_path=None) -> FastAPI:
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
    prompts_dir = Path(prompts_dir or PROMPTS_DIR)
    agent_url = os.environ.get("AGENT_URL", "http://agent:8002")

    def default_lab_post(body: dict) -> dict:
        r = httpx.post(f"{agent_url}/complete", json=body, timeout=120)
        r.raise_for_status()
        return r.json()

    lab_post = lab_post or default_lab_post
    app = FastAPI(title="chatbot replay")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def page():
        return PAGE.read_text(encoding="utf-8")

    @app.get("/api/status")
    def status():
        """What the agent is pointed at: written by the agent at startup, plus the policy file as it is now."""
        try:
            agent = json.loads(Path(settings.trace_db_path).with_name("agent_status.json").read_text())
        except (OSError, ValueError):
            agent = None
        url = agent and agent.get("calendar_url")
        policy = load_policy()
        return {"agent_reported": agent is not None, "calendar_url": url,
                "calendar_port": urlparse(url).port if url else None,
                "allow_writes": bool(agent and agent.get("allow_writes")),
                "policy_mode": policy.mode, "announce_results": policy.announce_results,
                "routes": load_channels().routes,
                "writing": bool(agent and agent.get("allow_writes")) and policy.mode == "live"}

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

    @app.get("/api/traces/{trace_id}/outbox")
    def trace_outbox(trace_id: str):
        return store.get_outbox(trace_id)

    @app.get("/api/outbox")
    def outbox():
        return store.list_outbox(limit=50)

    @app.get("/outbox", response_class=HTMLResponse)
    def outbox_page():
        return OUTBOX_PAGE.read_text(encoding="utf-8")

    @app.get("/api/traces/{trace_id}")
    def trace(trace_id: str):
        t = store.get_trace(trace_id)
        if t is None:
            raise HTTPException(404)
        return t

    # ---- prompt editor: view, render for a trace, try through the agent, save

    def prompt_dir(name: str) -> Path:
        d = prompts_dir / name
        if not re.fullmatch(r"[a-z0-9_-]+", name) or not d.is_dir():
            raise HTTPException(404, "unknown prompt")
        return d

    def render(template: str, trace_id: str) -> str:
        t = store.get_trace(trace_id)
        got = next((st for st in (t or {"steps": []})["steps"] if st["name"] == "agent_received"), None)
        if not got or not got["output"]:
            raise HTTPException(404, "this trace never reached the agent")
        o = got["output"]
        try:
            return render_user(template, o["member"], o["squad"], datetime.fromisoformat(o["sent_at"]), o["text"])
        except (KeyError, IndexError, ValueError):
            raise HTTPException(422, "template may only use {message_time}, {sender} and {message}")

    @app.get("/api/prompts/{name}")
    def get_prompt(name: str):
        prompt_dir(name)
        p = load_prompt(name, prompts_dir)
        return {"system": p.system, "user_template": p.user_template, "schema": p.schema, "version": p.version}

    @app.post("/api/prompts/{name}/render")
    def render_prompt(name: str, req: TryPrompt):
        prompt_dir(name)
        return {"user": render(req.user_template, req.trace_id)}

    def call_lab(body: dict) -> dict:
        try:
            return lab_post(body)
        except httpx.HTTPStatusError as exc:
            raise HTTPException(exc.response.status_code, "the agent refused the call (is PROMPT_LAB=1 set, and is the model known?)")
        except httpx.HTTPError:
            raise HTTPException(502, "the agent's prompt lab is not reachable (PROMPT_LAB=1 on the agent?)")

    @app.post("/api/prompts/{name}/try")
    def try_prompt(name: str, req: TryPrompt):
        prompt_dir(name)
        return call_lab({"system": req.system, "user": render(req.user_template, req.trace_id),
                         "schema": load_prompt(name, prompts_dir).schema, "model": req.model, "max_tokens": req.max_tokens})

    @app.post("/api/prompts/{name}/refine")
    def refine_prompt(name: str, req: RefineRequest):
        """Ask a model to suggest a better system prompt, using prompts/prompt_refinement.md."""
        prompt_dir(name)
        try:
            template = (prompts_dir / REFINE_TEMPLATE).read_text(encoding="utf-8")
        except OSError:
            raise HTTPException(404, f"prompts/{REFINE_TEMPLATE} not found")
        values = {"prompt_name": name, "model": req.model, "system_prompt": req.system, "user_message": req.user,
                  "response_schema": json.dumps(load_prompt(name, prompts_dir).schema, indent=2),
                  "response": req.response, "hints": req.hints.strip() or "(no hints given)"}
        unknown = sorted(set(re.findall(r"\{\{(\w+)\}\}", template)) - set(values))
        if unknown:
            raise HTTPException(422, f"{REFINE_TEMPLATE} uses unknown fields: {', '.join(unknown)}")
        # One pass, so text inside a value that looks like {{field}} is never filled in a second time.
        filled = re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)
        out = call_lab({"system": REFINE_SYSTEM, "user": filled, "schema": REFINE_SCHEMA, "model": REFINE_MODEL,
                        "max_tokens": 4096})
        if "error" in out:
            return out
        try:
            suggestion = json.loads(out["text"])
            return {**{k: out[k] for k in ("model", "input_tokens", "output_tokens", "cost_usd", "latency_ms")},
                    "system_prompt": suggestion["system_prompt"], "explanation": suggestion["explanation"]}
        except (ValueError, KeyError, TypeError):
            raise HTTPException(502, "the model's suggestion could not be read")

    @app.post("/api/prompts/{name}/evaluate")
    def evaluate_prompt(name: str, req: EvalRequest):
        """Run one or two system prompts over the labeled examples and score them.

        Each example goes through the same PII filter as a real message first, so what reaches the model
        matches production; any the filter would not pass are skipped.
        """
        if name not in evaluate.EVALUABLE:
            raise HTTPException(422, "only the intent and tier1 prompts can be scored")
        prompt_dir(name)
        if not 1 <= len(req.systems) <= 2:
            raise HTTPException(422, "give one or two prompts")
        try:
            cases = evaluate.load_cases(Path(cases_path or DEFAULT_CASES))
        except OSError:
            raise HTTPException(404, "labeled examples not found (is test_cases/ mounted?)")
        pii = PiiFilter(load_allowlist(), roster.name_tokens())
        people = {m.member: m for m in roster.people()}
        schema = load_prompt(name, prompts_dir).schema
        skip_confidence = float(os.environ.get("INTENT_SKIP_CONFIDENCE", "0.8"))
        rows, work = [], []
        for c in cases:
            m = people.get(c.sender)
            filtered = pii.apply(c.text, m.names[0] if m and m.names else "")
            row = {"id": c.example_id, "expected": "schedule change" if c.is_change else "not a change",
                   "text": str(filtered.text) if filtered.allowed else None, "results": {}}
            rows.append(row)
            if not filtered.allowed:
                row["skipped"] = f"the PII filter would not pass it ({filtered.outcome.value})"
                continue
            try:
                user = render_user(req.user_template, m.member if m else None, m.squad if m else None, c.sent_at, str(filtered.text))
            except (KeyError, IndexError, ValueError):
                raise HTTPException(422, "template may only use {message_time}, {sender} and {message}")
            work += [(v, c, row, user) for v in req.systems]

        def run(item):
            variant, c, row, user = item
            try:
                out = call_lab({"system": variant.system, "user": user, "schema": schema, "model": req.model,
                                "max_tokens": req.max_tokens})
            except HTTPException as exc:
                return variant.label, row, {"verdict": "error", "detail": str(exc.detail)}
            if "error" in out:
                return variant.label, row, {"verdict": "error", "detail": out["error"][:200]}
            r = evaluate.score(name, out["text"], c, skip_confidence)
            return variant.label, row, {**r, "cost": out.get("cost_usd") or 0}

        with ThreadPoolExecutor(max_workers=4) as pool:
            for label, row, result in pool.map(run, work):
                row["results"][label] = result
        summary = {}
        for v in req.systems:
            results = [r["results"][v.label] for r in rows if v.label in r["results"]]
            summary[v.label] = {**evaluate.summarize(results), "cost_usd": round(sum(x.get("cost", 0) for x in results), 6)}
        return {"prompt": name, "labels": [v.label for v in req.systems], "cases": rows, "summary": summary,
                "skipped": sum(1 for r in rows if "skipped" in r)}

    @app.put("/api/prompts/{name}")
    def save_prompt(name: str, req: PromptEdit):
        d = prompt_dir(name)
        try:
            req.user_template.format(message_time="", sender="", message="")
        except (KeyError, IndexError, ValueError):
            raise HTTPException(422, "template may only use {message_time}, {sender} and {message}")
        if not req.system.strip() or not req.user_template.strip():
            raise HTTPException(422, "both parts are required")
        for fname, text in (("system.md", req.system.strip()), ("user.md", req.user_template.rstrip("\n"))):
            tmp = d / (fname + ".tmp")
            tmp.write_text(text + "\n", encoding="utf-8")
            tmp.replace(d / fname)
        return {"version": load_prompt(name, prompts_dir).version}

    return app
