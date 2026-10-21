import pytest

from chatbot.config import load_allowlist, load_roster, Settings
from chatbot.pii.filter import Outcome, PiiFilter
from tests.pii_cases import KEEPS, KNOWN_GAPS, LEAKS


@pytest.fixture(scope="module")
def pii():
    roster = load_roster(Settings.from_env().roster_path)
    return PiiFilter(load_allowlist(), roster.name_tokens())


@pytest.mark.parametrize("text,forbidden", LEAKS)
def test_no_leak(pii, text, forbidden):
    r = pii.apply(text, sender_name="Alex Example")
    out = r.text or ""
    for f in forbidden:
        assert f.lower() not in out.lower(), f"{f!r} leaked: {out!r}"


@pytest.mark.parametrize("text,required", KEEPS)
def test_scheduling_content_survives(pii, text, required):
    r = pii.apply(text)
    assert r.allowed, r
    for q in required:
        assert q in r.text, f"{q!r} missing from {r.text!r}"


def test_roster_and_sender_names_become_member(pii):
    r = pii.apply("Sam Sample says 54 is short Friday night", sender_name="Quentin Zorn")
    assert r.outcome is Outcome.REDACTED
    assert "Sam" not in r.text and "Sample" not in r.text
    r = pii.apply("Quentin: 34 no crew tonight", sender_name="Quentin Zorn")
    assert "Quentin" not in r.text


def test_incident_chatter_is_dropped_with_reason(pii):
    r = pii.apply("Pt is a 72 yo male with chest pain")
    assert r.outcome is Outcome.DROPPED and r.text is None
    assert r.reason == "call_details_no_schedule_signal"


def test_plain_chatter_without_schedule_signal_is_dropped(pii):
    r = pii.apply("Great job everyone at the training")
    assert r.outcome is Outcome.DROPPED and r.reason == "no_schedule_signal"


def test_residual_long_number_is_uncertain(pii):
    r = pii.apply("54 crew tonight ref 99887766")
    assert r.outcome is Outcome.UNCERTAIN and r.text is None


def test_fails_closed_on_error(pii, monkeypatch):
    monkeypatch.setattr(pii, "_apply", lambda *a: 1 / 0)
    r = pii.apply("54 crew tonight")
    assert r.outcome is Outcome.UNCERTAIN and r.text is None and r.reason == "filter_error:ZeroDivisionError"


def test_too_long_is_uncertain(pii):
    assert pii.apply("54 crew " * 200).outcome is Outcome.UNCERTAIN


@pytest.mark.xfail(strict=True, reason="needs local NER; see KNOWN_GAPS")
@pytest.mark.parametrize("text,forbidden", KNOWN_GAPS)
def test_known_gaps(pii, text, forbidden):
    out = (pii.apply(text).text or "").lower()
    assert all(f.lower() not in out for f in forbidden) and pii.apply(text).allowed


def test_day_abbreviations_are_not_names(pii):
    r = pii.apply("42 can cover Sat night and Sun 0600")
    assert "[name]" not in r.text
