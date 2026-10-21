"""Runtime settings, read from environment variables, plus the roster and allowlist files."""

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).resolve().parents[2] / "config"))
PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", Path(__file__).resolve().parents[2] / "prompts"))
GROUP_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Member:
    sender_id: str  # primary GroupMe sender id (or a synthetic csv-N id when the roster has none)
    member: str  # pseudonym, e.g. "Member-7"; real names never leave the roster file
    squad: str
    names: tuple[str, ...] = ()  # every name this person has used; the first is the display name
    sender_ids: tuple[str, ...] = ()  # every sender id that maps to this person


@dataclass(frozen=True)
class Roster:
    members: dict[str, Member] = field(default_factory=dict)  # by sender_id

    def lookup(self, sender_id: str) -> Member | None:
        return self.members.get(sender_id)

    def people(self) -> list[Member]:
        """One entry per person, in roster order (several sender ids can map to one person)."""
        seen: dict[str, Member] = {}
        for m in self.members.values():
            seen.setdefault(m.member, m)
        return list(seen.values())

    def name_tokens(self) -> set[str]:
        """Every word of every member's real name, for scrubbing."""
        return {t for m in self.members.values() for n in m.names for t in n.split()}


@dataclass(frozen=True)
class Allowlist:
    squads: frozenset[str]
    words: frozenset[str]  # words that are never redacted, lowercased


@dataclass(frozen=True)
class Settings:
    trace_db_path: str
    queue_url: str
    sqs_endpoint_url: str | None
    roster_path: Path
    webhook_token: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        candidates = [CONFIG_DIR / n for n in ("roster.local.csv", "roster.local.json", "roster.example.json")]
        default_roster = next((c for c in candidates if c.exists()), candidates[-1])
        return cls(
            trace_db_path=os.environ.get("TRACE_DB_PATH", "data/trace.db"),
            queue_url=os.environ.get("QUEUE_URL", "http://localhost:9324/000000000000/chatbot-events"),
            sqs_endpoint_url=os.environ.get("SQS_ENDPOINT_URL", "http://localhost:9324") or None,
            roster_path=Path(os.environ.get("ROSTER_PATH", default_roster)),
            webhook_token=os.environ.get("WEBHOOK_TOKEN") or None,
        )


def load_roster(path: Path) -> Roster:
    """Read the roster from a .csv (name, squad[, sender_ids][, aliases]) or a .json file."""
    path = Path(path)
    return load_roster_csv(path) if path.suffix.lower() == ".csv" else load_roster_json(path)


def load_roster_json(path: Path) -> Roster:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    members = {
        m["sender_id"]: Member(m["sender_id"], m["member"], m["squad"], tuple(m.get("names", [])), (m["sender_id"],))
        for m in data["members"]
    }
    return Roster(members)


def load_roster_csv(path: Path) -> Roster:
    """Columns: name, squad, then optionally sender_ids and aliases (each ';'-separated).

    Rows get pseudonyms Member-1, Member-2, ... in file order, so keep the order stable. A person
    with no sender_ids gets a synthetic id (csv-N): fine for replay, but a live GroupMe sender will
    not match until the real ids are added (scripts/enrich_roster.py does that from an export).
    """
    members: dict[str, Member] = {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = [[c.strip() for c in r] for r in csv.reader(f) if any(c.strip() for c in r)]
    if rows and rows[0][0].lower() in ("name", "member"):
        rows = rows[1:]
    for n, row in enumerate(rows, 1):
        if len(row) < 2:
            raise ValueError(f"{path}: row {n} needs at least name and squad")
        ids = tuple(i for i in (row[2].split(";") if len(row) > 2 else []) if i) or (f"csv-{n}",)
        aliases = tuple(a for a in (row[3].split(";") if len(row) > 3 else []) if a and a != row[0])
        person = Member(ids[0], f"Member-{n}", row[1], (row[0], *aliases), ids)
        # Rows that share a sender id are the same person listed under two names: merge them.
        for known in {members[i].member: members[i] for i in ids if i in members}.values():
            if known.squad != person.squad:
                raise ValueError(f"{path}: row {n} shares a sender id with {known.member} but has a different squad")
            person = Member(known.sender_id, known.member, known.squad,
                            tuple(dict.fromkeys((*known.names, *person.names))),
                            tuple(dict.fromkeys((*known.sender_ids, *person.sender_ids))))
        for i in person.sender_ids:
            members[i] = person
    return Roster(members)


def load_allowlist(path: Path | None = None) -> Allowlist:
    data = json.loads(Path(path or CONFIG_DIR / "allowlist.json").read_text(encoding="utf-8"))
    words = {w.lower() for name in data["squad_names"] + data["vocabulary"] for w in name.split()}
    return Allowlist(frozenset(data["squads"]), frozenset(words))
