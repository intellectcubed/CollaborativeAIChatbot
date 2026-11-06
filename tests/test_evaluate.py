import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chatbot.config import Settings
from chatbot.replay import evaluate
from chatbot.replay.app import create_app as create_replay
from chatbot.tracing.store import TraceStore

REAL = Path(__file__).resolve().parents[1]
HEADER = ("example_id,message_id,timestamp_et,sender,text,context,type,squads,time_scope,ambiguity,classification,"
          "expected_action,squads,date,start_time,end_time,notes")
ROWS = [
    '1,m1,2025-03-04T14:10:00-05:00,Member-1,54 will not have a crew tonight 2200-0100,ctx,no_crew,54,partial,,Schedule Change,NoCrew,54,2025-03-04,2200,0100,',
    '2,m2,2025-03-04T15:00:00-05:00,Member-2,Reminder 35 is not available until 11,ctx,other,,,,Not Schedule Change,,,,,,',
    '3,m3,2025-03-05T09:00:00-05:00,Member-1,"34 adds noon, and again 1800",ctx,add_crew,34,,,Schedule Change,addShift,34,2025-03-05,1200,1400,',
    '3 (continued),,,,,,,,,,,addShift,34,2025-03-05,1800,2000,',
    '4,m4,2025-03-06T09:00:00-05:00,Member-1,I am tango,ctx,swap,,,,Schedule Change,AssignTango,42,2025-03-06,2300,0600,',
    '5,m5,2025-03-07T09:00:00-05:00,Member-1,unlabeled,ctx,,,,,,,,,,,',
]


@pytest.fixture
def csv_path(tmp_path):
    p = tmp_path / "cases.csv"
    p.write_text("\n".join([HEADER, *ROWS]) + "\n", encoding="utf-8")
    return p


def test_loader_uses_the_second_squads_column_and_joins_continuations(csv_path):
    cases = evaluate.load_cases(csv_path)
    assert [c.example_id for c in cases] == ["1", "2", "3", "4"]  # the unlabeled row is left out
    assert cases[0].expected == [("NoCrew", "54", "2025-03-04", "2200", "0100")]
    assert cases[2].expected == [("AddCrew", "34", "2025-03-05", "1200", "1400"), ("AddCrew", "34", "2025-03-05", "1800", "2000")]
    assert cases[1].is_change is False and cases[1].expected == []
    assert cases[3].scorable_tier1 is False  # AssignTango is not a command the model can produce


def test_the_committed_examples_load():
    cases = evaluate.load_cases(REAL / "test_cases" / "labeled_examples.csv")
    assert len(cases) >= 30 and any(c.is_change for c in cases) and any(not c.is_change for c in cases)


def intent(is_change, conf):
    return json.dumps({"is_schedule_change": is_change, "confidence": conf, "reason": "r"})


def test_intent_scoring_treats_a_missed_change_as_the_costly_mistake(csv_path):
    change, other = evaluate.load_cases(csv_path)[:2]
    assert evaluate.score_intent(intent(True, 0.9), change, 0.8)["verdict"] == "pass"
    assert evaluate.score_intent(intent(False, 0.95), change, 0.8)["verdict"] == "miss"
    assert evaluate.score_intent(intent(False, 0.5), change, 0.8)["verdict"] == "pass"  # not confident enough to skip
    assert evaluate.score_intent(intent(False, 0.95), other, 0.8)["verdict"] == "pass"
    assert evaluate.score_intent(intent(True, 0.9), other, 0.8)["verdict"] == "extra"
    assert evaluate.score_intent("nope", other, 0.8)["verdict"] == "error"


def tier1(commands, scheduling=True):
    return json.dumps({"is_scheduling": scheduling, "confidence": 0.9, "reasoning": "r", "commands": commands})


def cmd(action, squads, date="", start="", end="", question=""):
    return {"action": action, "squads": squads, "date": date, "start": start, "end": end, "question": question}


def test_tier1_scoring_compares_commands_as_sets_and_ignores_clarify(csv_path):
    one, other, two, tango = evaluate.load_cases(csv_path)
    good = tier1([cmd("NoCrew", ["54"], "2025-03-04", "2200", "0100")])
    assert evaluate.score_tier1(good, one)["verdict"] == "pass"
    assert evaluate.score_tier1(tier1([cmd("NoCrew", ["54"], "2025-03-04", "2200", "0100"), cmd("Clarify", [], question="?")]), one)["verdict"] == "pass"
    wrong = evaluate.score_tier1(tier1([cmd("NoCrew", ["54"], "2025-03-04", "2100", "0100")]), one)
    assert wrong["verdict"] == "fail" and "2100" in wrong["detail"] and "2200" in wrong["detail"]
    assert evaluate.score_tier1(tier1([], scheduling=False), other)["verdict"] == "pass"
    assert evaluate.score_tier1(tier1([cmd("NoCrew", ["54"], "2025-03-04", "2200", "0100")]), other)["verdict"] == "fail"
    both = tier1([cmd("AddCrew", ["34"], "2025-03-05", "1800", "2000"), cmd("AddCrew", ["34"], "2025-03-05", "1200", "1400")])
    assert evaluate.score_tier1(both, two)["verdict"] == "pass"
    assert evaluate.score_tier1(good, tango)["verdict"] == "skipped"


def test_summary_counts():
    s = evaluate.summarize([{"verdict": "pass"}, {"verdict": "pass"}, {"verdict": "fail"}, {"verdict": "skipped"}])
    assert (s["passed"], s["scored"]) == (2, 3)


# ---- the endpoint

@pytest.fixture
def client(tmp_path, csv_path):
    shutil.copytree(REAL / "prompts", tmp_path / "prompts")
    base = Settings.from_env()
    s = Settings(str(tmp_path / "t.db"), base.queue_url, None, base.roster_path, None)
    calls = []

    def lab_post(body):
        calls.append(body)
        # The "good" system prompt gets every case right; any other one says everything is a schedule change.
        good = body["system"] == "GOOD"
        wants_change = any(w in body["user"] for w in ("tonight", "noon", "tango"))
        text = intent(good and wants_change or not good, 0.9) if "is_schedule_change" in json.dumps(body["schema"]) else tier1([])
        return {"text": text, "model": body["model"], "cost_usd": 0.001}

    app = create_replay(s, TraceStore(s.trace_db_path), prompts_dir=tmp_path / "prompts", lab_post=lab_post, cases_path=csv_path)
    return TestClient(app), calls


EVAL = {"user_template": "Message time: {message_time}\nFrom: {sender}\nMessage: {message}", "model": "claude-haiku-5-5",
        "max_tokens": 100, "systems": [{"label": "current", "system": "BAD"}, {"label": "suggested", "system": "GOOD"}]}


def test_evaluate_compares_two_prompts_and_sends_only_filtered_text(client):
    c, calls = client
    r = c.post("/api/prompts/intent/evaluate", json=EVAL)
    assert r.status_code == 200
    body = r.json()
    assert body["labels"] == ["current", "suggested"] and len(body["cases"]) == 4
    assert body["summary"]["suggested"]["passed"] > body["summary"]["current"]["passed"]
    assert len(calls) == 8  # 4 examples x 2 prompts
    assert all("From: Member-" in call["user"] for call in calls)  # rendered like a real message


def test_evaluate_skips_what_the_pii_filter_would_not_pass(client, tmp_path):
    c, calls = client
    p = tmp_path / "pii.csv"
    p.write_text("\n".join([HEADER, "1,m1,2025-03-04T14:10:00-05:00,Member-1,Call 555-123-4567 about the 54 crew,c,x,,,,Schedule Change,NoCrew,54,2025-03-04,2200,0100,"]) + "\n")
    c.app.dependency_overrides = {}
    r = TestClient(create_replay(Settings.from_env(), TraceStore(str(tmp_path / "p.db")), prompts_dir=tmp_path / "prompts",
                                 lab_post=lambda b: calls.append(b) or {"text": intent(True, 0.9)}, cases_path=p)).post(
        "/api/prompts/intent/evaluate", json={**EVAL, "systems": EVAL["systems"][:1]})
    [row] = r.json()["cases"]
    assert "[phone]" in (row["text"] or "") or "skipped" in row
    assert not any("555-123-4567" in call["user"] for call in calls)


def test_evaluate_validates_its_input(client):
    c, _ = client
    assert c.post("/api/prompts/nope/evaluate", json=EVAL).status_code == 422  # only intent and tier1 can be scored
    assert c.post("/api/prompts/intent/evaluate", json={**EVAL, "systems": []}).status_code == 422
    assert c.post("/api/prompts/intent/evaluate", json={**EVAL, "user_template": "{bad}"}).status_code == 422


# ---- dates reported by the intent prompt

def intent_with_dates(refs, is_change=True, conf=0.9):
    blank = {"weekday": "", "relative": "", "qualifier": "", "month": 0, "day": 0}
    return json.dumps({"is_schedule_change": is_change, "confidence": conf, "reason": "r",
                       "date_references": [{**blank, **r} for r in refs]})


def test_intent_scoring_checks_the_resolved_dates(csv_path):
    one = evaluate.load_cases(csv_path)[0]  # sent Tuesday 2025-03-04, expects 2025-03-04
    ok = evaluate.score_intent(intent_with_dates([{"relative": "tonight"}]), one, 0.8)
    assert (ok["verdict"], ok["date"]) == ("pass", "ok")
    wrong = evaluate.score_intent(intent_with_dates([{"weekday": "friday"}]), one, 0.8)
    assert wrong["date"] == "wrong" and "expected 2025-03-04" in wrong["detail"] and "2025-03-07" in wrong["detail"]
    none = evaluate.score_intent(intent_with_dates([]), one, 0.8)  # the message names no date: the label's date is implied
    assert none["date"] == "none" and "no date named" in none["detail"]


def test_non_changes_are_not_date_scored_and_the_summary_counts_dates(csv_path):
    other = evaluate.load_cases(csv_path)[1]
    assert "date" not in evaluate.score_intent(intent_with_dates([], is_change=False), other, 0.8)
    s = evaluate.summarize([{"verdict": "pass", "date": "ok"}, {"verdict": "pass", "date": "wrong"},
                            {"verdict": "pass", "date": "none"}, {"verdict": "extra"}])
    assert (s["dates_scored"], s["dates_ok"], s["dates_none"]) == (2, 1, 1)
