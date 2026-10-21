"""Local PII filter. Runs in ingest, before anything is queued, traced, logged or sent to a model.

Deterministic and offline. Layers, in order:
  1. Sentences that look like 911 call details (incident vocabulary, ages) are removed.
     If nothing with a scheduling signal remains, the message is DROPPED.
  2. Pattern rules redact phones, emails, URLs, street addresses, dates of birth, incident numbers.
  3. Known member names (roster, sender) become [member]; other capitalised names become [name].
  4. Residual checks: anything still suspicious makes the result UNCERTAIN.

The filter fails closed: any exception yields UNCERTAIN and no text. Only PASS and REDACTED carry
text. Known gaps (see tests/test_pii.py KNOWN_GAPS): lowercase names, a single capitalised name
at the start of a sentence, call details in the same sentence as a scheduling request. Local NER
(Presidio with spaCy) is the planned next layer.
"""

import re
from dataclasses import dataclass, field
from enum import Enum

from chatbot.config import Allowlist
from chatbot.models import RedactedText

MAX_LENGTH = 1000


class Outcome(str, Enum):
    PASS = "pass"  # nothing needed redacting
    REDACTED = "redacted"
    DROPPED = "dropped"  # call details with no scheduling signal
    UNCERTAIN = "uncertain"  # the filter is not sure; flag for the admin


@dataclass(frozen=True)
class FilterResult:
    outcome: Outcome
    text: RedactedText | None
    reason: str | None = None
    redactions: dict[str, int] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.text is not None


PHONE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z0-9.]+\s+){1,3}"
    r"(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|ct|court|way|pl|place|ter|terrace|pkwy|hwy|highway)\b\.?",
    re.IGNORECASE,
)
DOB = re.compile(r"\b(?:dob|d\.o\.b\.?|born)\b[:\s]*\S+|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.IGNORECASE)
INCIDENT_NUMBER = re.compile(r"\b(?:incident|call|run|case|report|cad)\s*(?:#|no\.?|number)?\s*\d{3,}\b|#\s*\d{4,}", re.IGNORECASE)
MENTION = re.compile(r"@\w+(?:\s[A-Z]\w+)?")

AGE = re.compile(r"\b\d{1,3}\s?(?:y/?o|yo|yom|yof|yr|yrs|years?)(?:[\s-]*old)?\b", re.IGNORECASE)
INCIDENT_WORDS = re.compile(
    r"\bpatient|\bpt\b|\bmale\b|\bfemale\b|chest pain|cardiac|arrest|overdose|narcan|unresponsive|seizure|stroke|"
    r"trauma|dispatch|respond|transport|hospital|\bCPR\b|\bDOA\b|bleeding|\bfell\b|\bfall\b|unconscious|breathing|"
    r"diabet|intubat|\bDNR\b|crash|accident|fire\b|injur|\bsick\b|\bdied\b|\bdeceased\b|\bhx\b|\bc/o\b",
    re.IGNORECASE,
)

SCHEDULE_SIGNAL = re.compile(
    r"\bcrew|\bshift\b|\bcover|\bswap|\btrade\b|\btango\b|\bschedul|\bon duty\b|\boff duty\b|\bpick(?:ing)? up\b|"
    r"\bno one\b|\bnobody\b|\btonight\b|\btomorrow\b|\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b|"
    r"(?<!\d)(?:[01]\d|2[0-3])[0-5]\d(?!\d)|\b\d{1,2}\s?(?:am|pm)\b|\b(?:34|35|42|43|54)\b|"
    r"green knoll|finderne|manville|martinsville|somerville|\btruck\b|\bambulance\b|\bshort\b",
    re.IGNORECASE,
)

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[;\n]+")
CAPITALISED = re.compile(r"\b[A-Z][a-z]{2,}\b")
RESIDUAL_LONG_NUMBER = re.compile(r"\d{5,}")

# Capitalised words that are not names.
COMMON_WORDS = frozenset(
    """monday tuesday wednesday thursday friday saturday sunday january february march april may june july august
    september october november december i thanks thank please sorry hello hey hi ok okay yes yes no good morning
    mon tue tues wed thu thur thurs fri sat sun jan feb mar apr jun jul aug sep sept oct nov dec
    evening night tonight tomorrow today can could would should will does did are was were the and but also
    anyone someone everyone any all both who what when where why how""".split()
)


def _sub(pattern, repl, text, counts, key):
    text, n = pattern.subn(repl, text)
    if n:
        counts[key] = counts.get(key, 0) + n
    return text


class PiiFilter:
    def __init__(self, allowlist: Allowlist, roster_name_tokens=()):
        self.allowlist = allowlist
        self.roster_tokens = {t.lower() for t in roster_name_tokens if self._name_like(t)}

    def _name_like(self, token: str) -> bool:
        t = token.lower()
        return len(t) >= 3 and t.isalpha() and t not in self.allowlist.words and t not in COMMON_WORDS

    def apply(self, text: str | None, sender_name: str = "") -> FilterResult:
        """Filter one message. Never raises."""
        try:
            return self._apply(text, sender_name)
        except Exception as exc:  # fail closed
            return FilterResult(Outcome.UNCERTAIN, None, f"filter_error:{type(exc).__name__}")

    def _apply(self, text, sender_name):
        if not text or not text.strip():
            return FilterResult(Outcome.DROPPED, None, "empty")
        if len(text) > MAX_LENGTH:
            return FilterResult(Outcome.UNCERTAIN, None, "too_long")

        counts: dict[str, int] = {}
        kept = []
        for sentence in (s.strip() for s in SENTENCE_SPLIT.split(text)):
            if not sentence:
                continue
            if INCIDENT_WORDS.search(sentence) or AGE.search(sentence):
                counts["call_details"] = counts.get("call_details", 0) + 1
            else:
                kept.append(sentence)
        if not any(SCHEDULE_SIGNAL.search(s) for s in kept):
            return FilterResult(Outcome.DROPPED, None, "no_schedule_signal" if not counts else "call_details_no_schedule_signal", counts)

        names = set(self.roster_tokens) | {t.lower() for t in re.split(r"[\s.,_-]+", sender_name) if self._name_like(t)}
        out = []
        for sentence in kept:
            sentence = _sub(URL, "[link]", sentence, counts, "link")
            sentence = _sub(EMAIL, "[email]", sentence, counts, "email")
            sentence = _sub(PHONE, "[phone]", sentence, counts, "phone")
            sentence = _sub(DOB, "[dob]", sentence, counts, "dob")
            sentence = _sub(INCIDENT_NUMBER, "[incident]", sentence, counts, "incident_number")
            sentence = _sub(ADDRESS, "[address]", sentence, counts, "address")
            sentence = _sub(MENTION, "@member", sentence, counts, "mention")
            if names:
                pattern = re.compile(r"\b(?:" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")\b", re.IGNORECASE)
                sentence = _sub(pattern, "[member]", sentence, counts, "member_name")
            sentence = self._redact_unknown_names(sentence, counts)
            out.append(sentence)

        result = re.sub(r"\s+", " ", " ".join(out)).strip()
        if RESIDUAL_LONG_NUMBER.search(result):
            return FilterResult(Outcome.UNCERTAIN, None, "residual_long_number", counts)
        outcome = Outcome.REDACTED if counts else Outcome.PASS
        return FilterResult(outcome, RedactedText.mint(result), None, counts)

    def _redact_unknown_names(self, sentence, counts):
        def is_name(word):
            return self._name_like(word)

        def repl(m):
            word = m.group(0)
            at_start = m.start() == 0 or sentence[: m.start()].rstrip().endswith(("[member]", "[name]"))
            if not is_name(word):
                return word
            if at_start:
                # A lone capitalised word at the start is usually just a sentence start;
                # treat it as a name only when another capitalised name-like word follows.
                nxt = CAPITALISED.match(sentence[m.end():].lstrip())
                if not (nxt and is_name(nxt.group(0))):
                    return word
            counts["unknown_name"] = counts.get("unknown_name", 0) + 1
            return "[name]"

        return CAPITALISED.sub(repl, sentence)
