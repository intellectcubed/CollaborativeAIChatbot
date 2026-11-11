"""Replay tool: impersonate a member at a chosen time and post to the message processor.

It talks to ingest over HTTP exactly as GroupMe would (plus X-Source: replay) and only reads the
trace store. Raw text is never written here; responses carry only trace ids and redacted trace data.
The prompt editor is the one other thing it does: it saves prompts/<name>/{system,user}.md, and sends
trial calls through the agent (this container has no API key).
"""

import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from chatbot.channels import load_channels
from chatbot.agent.history import NO_HISTORY, history_text
from chatbot.agent.interpret import NO_DATES, render_user
from chatbot.agent.schedule_info import NO_SCHEDULE
from chatbot.config import CONFIG_DIR, GROUP_TZ, PROMPTS_DIR, Settings, load_allowlist, load_roster
from chatbot.interfaces.llm import PRICES
from chatbot.interfaces.llm_config import ConfigError, to_dict as llm_config_dict, dumps as dumps_llm_config
from chatbot.interfaces.llm_config import load as load_llm_config, parse as parse_llm_config
from chatbot.pii.filter import PiiFilter
from chatbot.policy import load_policy
from chatbot.replay import evaluate
from chatbot.prompts import load_prompt
from chatbot.tracing.store import TraceStore

PAGE = Path(__file__).with_name("page.html")
CLEAR_PHRASE = "clear test data"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_openrouter_models: dict = {"at": 0.0, "ids": [], "error": None, "params": {}}


def llm_config_path() -> Path:
    return Path(os.environ.get("LLM_CONFIG_PATH") or CONFIG_DIR / "llm.toml")


def lab_settings(component: str | None, model: str) -> dict:
    """Source, effort and extra for a prompt-lab call, from the component's saved config when it uses this model."""
    default = {"source": "anthropic", "effort": None, "extra": {}}
    if component is None:
        return default
    try:
        comp = load_llm_config(llm_config_path()).component(component)
    except ConfigError:
        return default
    if comp is None or comp.settings.model != model:
        return default
    return {"source": comp.settings.source, "effort": comp.settings.effort, "extra": dict(comp.settings.extra)}


_anthropic_models: dict = {"at": 0.0, "ids": [], "error": None}


def anthropic_model_ids() -> tuple[list[str], str | None]:
    """Anthropic's model list, asked for through the agent (the replay tool has no key). Cached for an hour.
    If it cannot be read, the models this code has prices for are offered instead, with the reason."""
    if time.time() - _anthropic_models["at"] > 3600:
        try:
            r = httpx.get(f"{os.environ.get('AGENT_URL', 'http://agent:8002')}/models/anthropic", timeout=20)
            r.raise_for_status()
            _anthropic_models.update(at=time.time(), ids=r.json()["models"], error=None)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            _anthropic_models.update(at=time.time(), ids=sorted(PRICES),
                                     error=f"showing the priced models: the agent's model list is unavailable ({type(exc).__name__})")
    return _anthropic_models["ids"], _anthropic_models["error"]


def openrouter_model_ids() -> tuple[list[str], str | None]:
    """All OpenRouter model IDs, fetched at most once an hour. The list is public, so no key is needed."""
    if time.time() - _openrouter_models["at"] > 3600:
        try:
            r = httpx.get(OPENROUTER_MODELS_URL, timeout=20)
            r.raise_for_status()
            data = r.json()["data"]
            _openrouter_models.update(at=time.time(), ids=sorted(m["id"] for m in data), error=None,
                                      params={m["id"]: m.get("supported_parameters") or [] for m in data})
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            _openrouter_models.update(at=time.time(), error=f"could not load the OpenRouter list: {type(exc).__name__}")
    return _openrouter_models["ids"], _openrouter_models["error"]
OUTBOX_PAGE = Path(__file__).with_name("outbox.html")


class SendRequest(BaseModel):
    sender_id: str
    text: str
    sent_at: datetime | None = None  # naive values are read as America/New_York; default is now
    live_calendar: bool = True  # off: the calendar is only previewed for this message


class PromptEdit(BaseModel):
    system: str
    user_template: str


class ModelOverride(BaseModel):
    """A model picked in the prompt popup for this session only. It is never written to config/llm.toml."""

    source: Literal["anthropic", "openrouter"]
    model: str
    effort: str | None = None


class TryPrompt(PromptEdit):
    trace_id: str
    model: str
    max_tokens: int
    override: ModelOverride | None = None


class RefineRequest(BaseModel):
    system: str  # the system prompt as it is in the editor now
    user: str  # the user message that was sent
    response: str  # the response to fix: the latest one shown
    hints: str
    model: str  # the model the prompt runs on, so the suggestion is written for it
    override: ModelOverride | None = None
    max_tokens: int = Field(default=4096, ge=1, le=8192)  # the room for the rewritten prompt; set in the Suggest box


class SystemVariant(BaseModel):
    label: str
    system: str


class EvalRequest(BaseModel):
    user_template: str
    model: str
    max_tokens: int
    systems: list[SystemVariant]  # one prompt to score, or two to compare (e.g. current and suggested)
    override: ModelOverride | None = None


DEFAULT_CASES = Path(os.environ.get("TEST_CASES_PATH", Path(__file__).resolve().parents[3] / "test_cases" / "labeled_examples.csv"))
SCENARIOS = Path(os.environ.get("SCENARIOS_PATH", Path(__file__).resolve().parents[3] / "test_cases" / "scenarios"))
HISTORY_CLEAR_PHRASE = "clear history"


class HistoryIn(BaseModel):
    """One row of the chat history, as the History tab sends it. Text is raw here and is filtered before storing."""

    kind: str  # member | bot
    squad: str
    speaker: str  # a roster pseudonym for a member line; 'bot' for a bot line
    addressee: str | None = None  # bot lines only: the roster pseudonym it was addressed to
    sent_at: datetime  # naive values are read as America/New_York
    text: str
def llm_summary() -> dict:
    """One line for the banner: which model each component uses, and the budget."""
    try:
        cfg = load_llm_config(llm_config_path())
    except ConfigError as exc:
        return {"error": str(exc)}
    comps = {n: f"{c.settings.source} {c.settings.model}" + (f" effort {c.settings.effort}" if c.settings.effort else "")
             for n, c in cfg.components.items()}
    return {"components": comps, "per_message_usd": cfg.budget.per_message_usd, "per_day_usd": cfg.budget.per_day_usd,
            "kill_switch": cfg.budget.kill_switch}


FIELDS_MESSAGE = "template may only use {message_time}, {sender}, {message}, {dates}, {schedule} and {history}"
REFINE_TEMPLATE = "prompt_refinement.md"
REFINE_MODEL = os.environ.get("REFINE_MODEL", "claude-sonnet-5-5")
REFINE_SYSTEM = "Follow the instructions in the user message. Respond only with the JSON object they ask for."
REFINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["system_prompt", "explanation"],
                 "properties": {"system_prompt": {"type": "string"}, "explanation": {"type": "string"}}}


def create_app(settings=None, store=None, roster=None, post=None, prompts_dir=None, lab_post=None, cases_path=None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = store or TraceStore()
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
        return {"agent_reported": agent is not None, "calendar_url": url, "llm": llm_summary(),
                "calendar_port": urlparse(url).port if url else None,
                "allow_writes": bool(agent and agent.get("allow_writes")),
                "policy_mode": policy.mode, "announce_results": policy.announce_results,
                "routes": load_channels().routes,
                "writing": bool(agent and agent.get("allow_writes")) and policy.mode == "live"}

    @app.get("/api/llm-config")
    def get_llm_config():
        try:
            return llm_config_dict(load_llm_config(llm_config_path()))
        except ConfigError as exc:
            raise HTTPException(422, str(exc))

    @app.put("/api/llm-config")
    def save_llm_config(body: dict):
        """Developer mode only: the live agent never writes this file. Validated in full before it is written."""
        try:
            parse_llm_config(body)
        except ConfigError as exc:
            raise HTTPException(422, str(exc))
        path = llm_config_path()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(dumps_llm_config(body), encoding="utf-8")
        tmp.replace(path)
        return {"saved": True}

    @app.get("/api/llm-models")
    def llm_models():
        ids, error = openrouter_model_ids()
        anthropic, anthropic_error = anthropic_model_ids()
        return {"anthropic": anthropic, "anthropic_error": anthropic_error, "openrouter": ids, "openrouter_error": error,
                "openrouter_params": _openrouter_models["params"]}

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
            "live_calendar": req.live_calendar,
        }
        try:
            return post(payload)
        except httpx.HTTPError:
            raise HTTPException(502, "message processor unavailable")

    @app.post("/api/clear")
    def clear_test_data(body: dict):
        """Empty the test database: traces, outbox and chat history. The store refuses the production database."""
        if body.get("confirm") != CLEAR_PHRASE:
            raise HTTPException(422, f"send confirm = '{CLEAR_PHRASE}' to clear")
        try:
            store.clear()
        except RuntimeError as exc:
            raise HTTPException(403, str(exc))
        return {"cleared": True}

    # ---- chat history: the stored messages the next message's prompt sees. Developer workbench only; production
    # never edits history. Every row is checked and filtered like a real message before it is stored.

    pii = PiiFilter(load_allowlist(), roster.name_tokens())
    pseudonyms = {m.member for m in roster.people()}

    def checked_message(req: "HistoryIn") -> dict:
        if req.kind not in ("member", "bot"):
            raise HTTPException(422, "kind must be member or bot")
        if not req.squad.strip():
            raise HTTPException(422, "squad is required")
        if req.kind == "member" and req.speaker not in pseudonyms:
            raise HTTPException(422, "a member line needs a roster pseudonym as its speaker")
        if req.kind == "bot" and (req.speaker != "bot" or req.addressee not in pseudonyms):
            raise HTTPException(422, "a bot line is from 'bot' to a roster pseudonym")
        if not req.text.strip():
            raise HTTPException(422, "text is required")
        filtered = pii.apply(req.text, "", require_signal=False)  # history is context: keep ordinary chat lines
        if not filtered.allowed:
            raise HTTPException(422, f"the PII filter would not pass this text ({filtered.outcome.value})")
        when = req.sent_at if req.sent_at.tzinfo else req.sent_at.replace(tzinfo=GROUP_TZ)
        return {"kind": req.kind, "squad": req.squad.strip(), "speaker": req.speaker,
                "addressee": req.addressee if req.kind == "bot" else None, "sent_at": when,
                "text": filtered.text}  # the filter's own RedactedText

    @app.get("/api/history")
    def list_history(squad: str | None = None):
        return [{**r, "sent_at": r["sent_at"].isoformat()} for r in store.list_messages(squad or None)]

    @app.get("/api/history/preview")
    def preview_history(squad: str, until: datetime):
        """The exact history section a message from this squad at this time would be given."""
        when = until if until.tzinfo else until.replace(tzinfo=GROUP_TZ)
        return {"text": history_text(store, squad, when, None)}

    @app.post("/api/history")
    def add_history(req: "HistoryIn"):
        m = checked_message(req)
        return {"id": store.record_message(source="replay", **m)}

    @app.put("/api/history/{message_id}")
    def edit_history(message_id: int, req: "HistoryIn"):
        if not store.update_message(message_id, **checked_message(req)):
            raise HTTPException(404, "no such history row")
        return {"updated": True}

    @app.delete("/api/history/{message_id}")
    def delete_history(message_id: int):
        if not store.delete_message(message_id):
            raise HTTPException(404, "no such history row")
        return {"deleted": True}

    @app.post("/api/history/clear")
    def clear_history(body: dict):
        """Empty the chat history only (not the traces). Separate from clearing test data and from loading a scenario."""
        if body.get("confirm") != HISTORY_CLEAR_PHRASE:
            raise HTTPException(422, f"send confirm = '{HISTORY_CLEAR_PHRASE}' to clear the history")
        try:
            store.clear_history()
        except RuntimeError as exc:
            raise HTTPException(403, str(exc))
        return {"cleared": True}

    # ---- scenarios: files in test_cases/scenarios, played from the Batch tab

    def scenario_file(name: str) -> Path:
        p = SCENARIOS / f"{name}.json"
        if not re.fullmatch(r"[a-z0-9_-]+", name) or not p.is_file():
            raise HTTPException(404, "unknown scenario")
        return p

    def read_scenario(name: str) -> dict:
        try:
            return json.loads(scenario_file(name).read_text(encoding="utf-8"))
        except ValueError as exc:
            raise HTTPException(422, f"scenario file is not valid JSON: {exc}")

    @app.get("/api/scenarios")
    def list_scenarios():
        out = []
        for p in sorted(SCENARIOS.glob("*.json")) if SCENARIOS.is_dir() else []:
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                s = {}
            out.append({"name": p.stem, "description": s.get("description", ""),
                        "history": len(s.get("history", [])), "messages": len(s.get("messages", []))})
        return out

    @app.get("/api/scenarios/{name}")
    def get_scenario(name: str):
        return read_scenario(name)

    @app.post("/api/scenarios/{name}/load-history")
    def load_scenario_history(name: str):
        """Add the scenario's history rows to the chat history. Adds only: clear the history first if you want that.
        Every row is checked before any is stored."""
        s = read_scenario(name)
        try:
            rows = [HistoryIn.model_validate(h) for h in s.get("history", [])]
        except ValueError as exc:
            raise HTTPException(422, f"a history row is not valid: {exc}")
        checked = [checked_message(r) for r in rows]
        for m in checked:
            store.record_message(source="replay", **m)
        return {"added": len(checked)}

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
        intent_step = next((st for st in t["steps"] if st["name"] == "intent"), None)
        dates = ((intent_step or {}).get("output") or {}).get("dates_text") or NO_DATES
        schedule_step = next((st for st in t["steps"] if st["name"] == "schedule"), None)
        schedule = ((schedule_step or {}).get("output") or {}).get("schedule_text") or NO_SCHEDULE
        # The history the agent actually used for this message, as its trace recorded it.
        history_step = next((st for st in t["steps"] if st["name"] == "history"), None)
        history = ((history_step or {}).get("output") or {}).get("text") or NO_HISTORY
        try:
            return render_user(template, o["member"], o["squad"], datetime.fromisoformat(o["sent_at"]), o["text"], dates,
                               schedule, history)
        except (KeyError, IndexError, ValueError):
            raise HTTPException(422, FIELDS_MESSAGE)

    @app.get("/api/prompts/{name}")
    def get_prompt(name: str):
        prompt_dir(name)
        p = load_prompt(name, prompts_dir)
        return {"system": p.system, "user_template": p.user_template, "schema": p.schema, "version": p.version}

    @app.post("/api/prompts/{name}/render")
    def render_prompt(name: str, req: TryPrompt):
        prompt_dir(name)
        return {"user": render(req.user_template, req.trace_id)}

    def call_lab(body: dict, component: str | None = None, override: ModelOverride | None = None) -> dict:
        if override is not None:
            body = {**body, "model": override.model}
            settings = {"source": override.source, "effort": override.effort, "extra": {}}
        else:
            settings = lab_settings(component, body["model"])
        body = {**body, **settings}
        try:
            return lab_post(body)
        except httpx.HTTPStatusError as exc:
            try:
                reason = exc.response.json().get("detail", "")
            except ValueError:
                reason = ""
            raise HTTPException(exc.response.status_code, f"the agent refused the call: {reason or 'is PROMPT_LAB=1 set?'}")
        except httpx.HTTPError:
            raise HTTPException(502, "the agent's prompt lab is not reachable (PROMPT_LAB=1 on the agent?)")

    @app.post("/api/prompts/{name}/try")
    def try_prompt(name: str, req: TryPrompt):
        prompt_dir(name)
        return call_lab({"system": req.system, "user": render(req.user_template, req.trace_id),
                         "schema": load_prompt(name, prompts_dir).schema, "model": req.model, "max_tokens": req.max_tokens},
                        component=name, override=req.override)

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
                        "max_tokens": req.max_tokens}, override=req.override)
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
                raise HTTPException(422, FIELDS_MESSAGE)
            work += [(v, c, row, user) for v in req.systems]

        def run(item):
            variant, c, row, user = item
            try:
                out = call_lab({"system": variant.system, "user": user, "schema": schema, "model": req.model,
                                "max_tokens": req.max_tokens}, component=name, override=req.override)
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
            req.user_template.format(message_time="", sender="", message="", dates="", schedule="", history="")
        except (KeyError, IndexError, ValueError):
            raise HTTPException(422, FIELDS_MESSAGE)
        if not req.system.strip() or not req.user_template.strip():
            raise HTTPException(422, "both parts are required")
        for fname, text in (("system.md", req.system.strip()), ("user.md", req.user_template.rstrip("\n"))):
            tmp = d / (fname + ".tmp")
            tmp.write_text(text + "\n", encoding="utf-8")
            tmp.replace(d / fname)
        return {"version": load_prompt(name, prompts_dir).version}

    return app
