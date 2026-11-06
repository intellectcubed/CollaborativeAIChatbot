"""Score a prompt against the labeled examples (test_cases/labeled_examples.csv).

Used by the replay tool's prompt editor so a prompt change can be checked for regressions before it
is saved. Pure functions: no model calls and no files except the CSV.

Two prompts can be scored:
  intent: did the gate behave? A real schedule change that the gate would skip is a MISS (the costly
          mistake); a non-change that is let through is only a wasted Tier 1 call. The date words it
          reports are also resolved to dates and checked against the dates the example expects.
  tier1: are the commands right? Compared as sets of (action, squad, date, start, end); Clarify
          commands are ignored. A message that is not a schedule change passes if it yields no commands.
"""

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from chatbot.agent import dates as dates_mod
from chatbot.agent import intent as intent_mod
from chatbot.agent import interpret as interpret_mod

ACTIONS = {"NoCrew": "NoCrew", "addShift": "AddCrew"}  # CSV name -> command name; other labels are not scorable
EVALUABLE = ("intent", "tier1")


@dataclass
class Case:
    example_id: str
    sent_at: datetime
    sender: str  # roster pseudonym, e.g. Member-10
    text: str
    is_change: bool
    expected: list[tuple] = field(default_factory=list)  # (action, squad, date, start, end)
    scorable_tier1: bool = True


def load_cases(path: Path) -> list[Case]:
    """Read the CSV. The header repeats 'squads' (message context, then expected squads): the second is the answer.
    A row with no timestamp ("4 (continued)") continues the previous example's expected commands."""
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    seen: dict[str, int] = {}
    header = []
    for h in rows[0]:
        seen[h] = seen.get(h, 0) + 1
        header.append(h if seen[h] == 1 else f"{h}_{seen[h]}")
    cases: list[Case] = []
    for raw in rows[1:]:
        r = dict(zip(header, raw))
        expected = None
        action = r["expected_action"].strip()
        if action:
            squads = [s.strip() for s in r["squads_2"].replace(";", ",").split(",") if s.strip()]
            expected = [(ACTIONS.get(action, action), s, r["date"].strip(), r["start_time"].strip(), r["end_time"].strip()) for s in squads]
        if not r["timestamp_et"].strip() or not r["classification"].strip():
            if cases and expected:  # a continuation row ("4 (continued)")
                cases[-1].expected += expected
                cases[-1].scorable_tier1 &= action in ACTIONS
            continue
        case = Case(r["example_id"], datetime.fromisoformat(r["timestamp_et"]), r["sender"], r["text"],
                    r["classification"].strip() == "Schedule Change", expected or [], not action or action in ACTIONS)
        cases.append(case)
    return cases


def score_intent(text: str, case: Case, skip_confidence: float) -> dict:
    try:
        verdict = intent_mod.parse(text)
    except ValueError:
        return {"verdict": "error", "detail": "unreadable response"}
    skipped = intent_mod.should_skip(verdict, skip_confidence)
    got = f"{'schedule change' if verdict.is_schedule_change else 'not a change'} ({verdict.confidence:.2f})" + (", skipped" if skipped else "")
    date_check = _score_dates(verdict, case)
    if date_check:
        got += f" · {'no date named' if date_check['date'] == 'none' else 'dates ' + date_check['date']}" + (f" (expected {date_check['want']}, got {date_check['got']})" if date_check["date"] == "wrong" else "")
    extra = {"date": date_check["date"]} if date_check else {}
    if case.is_change and skipped:
        return {"verdict": "miss", "detail": got, **extra}  # the costly mistake: a real change would never reach Tier 1
    if not case.is_change and not skipped:
        return {"verdict": "extra", "detail": got, **extra}  # harmless: only costs a Tier 1 call
    return {"verdict": "pass", "detail": got, **extra}


def _score_dates(verdict, case: Case) -> dict | None:
    """Did the reported date words resolve to every date the example expects? Only for real changes with dates."""
    want = sorted({e[2] for e in case.expected if e[2]})
    if not (case.is_change and want):
        return None
    resolution = dates_mod.resolve(verdict.date_references, case.sent_at)
    if not resolution.items:
        return {"date": "none"}  # most real messages name no date at all; the label's date is implied, so this is not an error
    got = sorted({d.isoformat() for d in resolution.dates})
    return {"date": "ok" if set(want) <= set(got) else "wrong", "want": ", ".join(want), "got": ", ".join(got)}


def _commands(interpretation) -> set[tuple]:
    out = set()
    for c in interpretation.commands:
        if c.action in ("NoCrew", "AddCrew"):
            out |= {(c.action, s, c.date, c.start, c.end) for s in c.squads}
    return out


def score_tier1(text: str, case: Case) -> dict:
    if not case.scorable_tier1:
        return {"verdict": "skipped", "detail": "expected answer is not a NoCrew/AddCrew command"}
    try:
        got = interpret_mod.parse(text)
    except ValueError:
        return {"verdict": "error", "detail": "unreadable response"}
    commands = _commands(got) if got.is_scheduling else set()
    want = set(case.expected) if case.is_change else set()
    if commands == want:
        return {"verdict": "pass", "detail": f"{len(commands)} command(s)" if commands else "no commands"}
    show = lambda s: "; ".join(" ".join(c) for c in sorted(s)) or "none"
    return {"verdict": "fail", "detail": f"expected {show(want)} / got {show(commands)}"}


def score(name: str, text: str, case: Case, skip_confidence: float) -> dict:
    return score_intent(text, case, skip_confidence) if name == "intent" else score_tier1(text, case)


def summarize(results: list[dict]) -> dict:
    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    scored = sum(v for k, v in counts.items() if k != "skipped")
    dates = [r["date"] for r in results if r.get("date")]
    return {"counts": counts, "scored": scored, "passed": counts.get("pass", 0),
            "dates_scored": dates.count("ok") + dates.count("wrong"), "dates_ok": dates.count("ok"), "dates_none": dates.count("none")}
