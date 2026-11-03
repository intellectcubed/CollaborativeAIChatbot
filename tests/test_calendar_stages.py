from datetime import date, datetime

import pytest

from chatbot.agent.worker import handle
from chatbot.models import Event, RedactedText
from chatbot.policy import Policy, load_policy
from chatbot.schedule.client import CalendarUnavailable
from chatbot.schedule.mock import MockCalendar
from chatbot.tracing.store import TraceStore
from tests.fakes import FakeLlm, cmd, reply

MONDAY = date(2026, 11, 2)
SATURDAY = date(2026, 11, 7)
DRY = Policy(mode="dry_run")
LIVE = Policy(mode="live")


@pytest.fixture
def store(tmp_path):
    s = TraceStore(tmp_path / "t.db")
    s.start_trace("t1", "replay", "u", "x")
    return s


@pytest.fixture
def cal():
    c = MockCalendar()
    c.seed(MONDAY, 42, "1800", "0600")
    c.seed(MONDAY, 43, "1800", "0000")
    return c


@pytest.fixture(autouse=True)
def allow_writes(monkeypatch):
    monkeypatch.setenv("CALENDAR_ALLOW_WRITES", "1")


def event(squad="42"):
    return Event(trace_id="t1", source="replay", group_id="g", sender_id="u", member="Member-1", squad=squad,
                 sent_at=datetime.fromisoformat("2026-11-02T14:10:00-05:00"), text=RedactedText.mint("msg"), pii_outcome="pass")


def run(store, cal, commands, policy=DRY, confidence=0.95, squad="42"):
    handle(event(squad), store, FakeLlm(reply(commands=commands, confidence=confidence)), calendar=cal, policy=policy)
    t = store.get_trace("t1")
    return t["status"], {s["name"]: s for s in t["steps"]}


def coverage(cal, squad=42):
    return cal.get_day(MONDAY).coverage().get(squad)


NO_CREW = cmd("NoCrew", ["42"], "2026-11-02", "2200", "0100")


def test_dry_run_previews_and_never_writes(store, cal):
    status, st = run(store, cal, [NO_CREW])
    assert status == "done" and coverage(cal) == [(1080, 1800)]
    assert [c for c in cal.calls if c[0] == "noCrew"] == [("noCrew", MONDAY, "2200", "0100", 42, True)]  # preview=True
    assert st["reply"]["output"]["lines"] == ["Would call NoCrew squad 42, 2026-11-02 2200-0100"]
    assert st["apply"]["output"]["mode"] == "dry_run" and st["apply"]["output"]["actions"][0]["status"] == "previewed"
    assert st["gate"]["output"]["decision"] == "apply"


def test_live_applies_and_records_the_change_id(store, cal):
    status, st = run(store, cal, [NO_CREW], policy=LIVE)
    assert status == "done" and coverage(cal) == [(1080, 1320), (1500, 1800)]
    action = st["apply"]["output"]["actions"][0]
    assert action["status"] == "applied" and action["change_id"] and "change" in st["reply"]["output"]["lines"][0]


def test_live_needs_both_the_policy_and_the_environment(store, cal, monkeypatch):
    monkeypatch.delenv("CALENDAR_ALLOW_WRITES")
    status, st = run(store, cal, [NO_CREW], policy=LIVE)
    assert coverage(cal) == [(1080, 1800)] and st["apply"]["output"]["mode"] == "dry_run"


def test_without_a_calendar_nothing_changes_from_before(store):
    status, st = run(store, None, [NO_CREW])
    assert status == "done" and "validate" not in st and st["reply"]["output"]["lines"] == [
        "Call NoCrew on calendar for date 2026-11-02 (squad 42, 2200-0100)"]


def test_not_scheduled_is_a_no_op_and_nothing_is_called(store, cal):
    status, st = run(store, cal, [cmd("NoCrew", ["43"], "2026-11-02", "0200", "0300")], squad="43")
    assert st["reply"]["output"]["lines"] == ["No change: squad 43 is not scheduled 0200-0300 on 2026-11-02"]
    assert "apply" not in st and not [c for c in cal.calls if c[0] == "noCrew"]


def test_outside_a_shift_window_asks_the_sender(store, cal):
    status, st = run(store, cal, [cmd("NoCrew", ["42"], "2026-11-02", "1700", "1800")])
    [line] = st["reply"]["output"]["lines"]
    assert line.startswith("Ask sender: 1700-1800 is not a shift time on 2026-11-02; shifts that day run 1800-0600")
    assert "apply" not in st


def test_weekend_day_hours_are_a_real_window(store, cal):
    status, st = run(store, cal, [cmd("AddCrew", ["42"], "2026-11-07", "0600", "1800")])
    assert st["apply"]["output"]["actions"][0]["status"] == "previewed"


def test_partial_overlap_applies_and_says_so(store, cal):
    status, st = run(store, cal, [cmd("NoCrew", ["43"], "2026-11-02", "2300", "0100")], squad="43")
    assert "only part of that time was scheduled" in st["reply"]["output"]["lines"][0]


def test_another_squads_shift_is_blocked(store, cal):
    status, st = run(store, cal, [cmd("NoCrew", ["43"], "2026-11-02", "2300", "0000")], squad="42")
    assert status == "done" and st["reply"]["output"]["lines"][0].startswith("Not applied: NoCrew squad 43")
    assert "apply" not in st and not [c for c in cal.calls if c[0] == "noCrew"]


def test_cross_squad_edit_allowed_by_policy(store, cal):
    run(store, cal, [cmd("NoCrew", ["43"], "2026-11-02", "2300", "0000")], squad="42",
        policy=Policy(allow_cross_squad_edits=True))
    assert [c for c in cal.calls if c[0] == "noCrew"]


def test_unknown_sender_squad_is_blocked(store, cal):
    status, st = run(store, cal, [NO_CREW], squad=None)
    assert st["validate"]["output"]["actions"][0]["reason"] == "unknown_sender"


@pytest.mark.parametrize("command,reason", [
    (cmd("NoCrew", ["42"], "", "2200", "0100"), "missing_date"),
    (cmd("NoCrew", ["42"], "2026-11-02", "", ""), "missing_hours"),
    (cmd("NoCrew", ["99"], "2026-11-02", "2200", "0100"), "unknown_squad"),
    (cmd("NoCrew", [], "2026-11-02", "2200", "0100"), "unknown_squad"),
])
def test_missing_or_unknown_details_ask_the_sender(store, cal, command, reason):
    status, st = run(store, cal, [command])
    assert st["validate"]["output"]["actions"][0]["reason"] == reason and "apply" not in st


def test_model_clarification_passes_through(store, cal):
    status, st = run(store, cal, [cmd("Clarify", question="Which night do you mean?")])
    assert st["reply"]["output"]["lines"] == ["Ask sender: Which night do you mean?"] and "gate" not in st


def test_a_multi_squad_command_becomes_one_action_per_squad(store, cal):
    status, st = run(store, cal, [cmd("AddCrew", ["42", "43"], "2026-11-07", "0600", "1800")], policy=Policy(allow_cross_squad_edits=True))
    assert [a["squad"] for a in st["validate"]["output"]["actions"]] == [42, 43]


@pytest.mark.parametrize("confidence,decision,status,start", [
    (0.95, "apply", "done", "Called"),
    (0.7, "confirm", "done", "Ask sender to confirm before changing the calendar:"),
    (0.3, "flag", "flagged", "Flag for admin (low confidence 0.30):"),
])
def test_confidence_bands(store, cal, confidence, decision, status, start):
    final, st = run(store, cal, [NO_CREW], policy=LIVE, confidence=confidence)
    assert final == status and st["gate"]["output"]["decision"] == decision
    assert st["reply"]["output"]["lines"][0].startswith(start)
    assert (coverage(cal) == [(1080, 1800)]) == (decision != "apply")  # only a confident decision changes the calendar


def test_thresholds_come_from_the_policy(store, cal):
    final, st = run(store, cal, [NO_CREW], confidence=0.7, policy=Policy(apply_min_confidence=0.6))
    assert st["gate"]["output"]["decision"] == "apply"


def test_live_failure_rolls_back_what_was_applied(store, cal, monkeypatch):
    real = cal.no_crew
    seen = []

    def flaky(shift_date, start, end, squad, preview=False):
        seen.append(squad)
        if len(seen) == 2:
            raise RuntimeError("calendar rejected it")
        return real(shift_date, start, end, squad, preview)

    monkeypatch.setattr(cal, "no_crew", flaky)
    cmds = [cmd("NoCrew", ["42"], "2026-11-02", "2200", "0100"), cmd("NoCrew", ["42"], "2026-11-02", "0200", "0300")]
    status, st = run(store, cal, cmds, policy=LIVE)
    assert status == "error" and coverage(cal) == [(1080, 1800)]  # first change rolled back
    assert [a["status"] for a in st["apply"]["output"]["actions"]] == ["rolled_back", "failed"]


def test_calendar_outage_before_any_write_is_retried_by_the_queue(store, cal, monkeypatch):
    monkeypatch.setattr(cal, "get_day", lambda d: (_ for _ in ()).throw(CalendarUnavailable("down")))
    with pytest.raises(CalendarUnavailable):
        run(store, cal, [NO_CREW])
    assert store.get_trace("t1")["status"] == "received"


def test_calendar_outage_at_the_first_write_is_retried(store, cal, monkeypatch):
    monkeypatch.setattr(cal, "no_crew", lambda *a, **k: (_ for _ in ()).throw(CalendarUnavailable("down")))
    with pytest.raises(CalendarUnavailable):
        run(store, cal, [NO_CREW], policy=LIVE)


def test_policy_file_defaults_and_validation(tmp_path):
    assert load_policy(tmp_path / "missing.toml") == Policy()
    bad = tmp_path / "p.toml"
    bad.write_text('mode = "yolo"\n')
    assert load_policy(bad).mode == "dry_run"
    bad.write_text("apply_min_confidence = 0.4\nask_min_confidence = 0.8\n")
    assert load_policy(bad) == Policy()
    ok = tmp_path / "ok.toml"
    ok.write_text('mode = "live"\napply_min_confidence = 0.8\nask_min_confidence = 0.4\nallow_cross_squad_edits = true\n')
    p = load_policy(ok)
    assert (p.mode, p.apply_min_confidence, p.ask_min_confidence, p.allow_cross_squad_edits) == ("live", 0.8, 0.4, True)


def test_the_committed_policy_file_is_a_dry_run():
    assert load_policy().mode == "dry_run"
