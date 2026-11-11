"""Postgres trace store shared by ingest, the agent and the replay tool.

Every trace step, every model call, every outbox row and (later) the chat history lives in Postgres.
Each service connects to the database named by DATABASE_URL (see chatbot.config.database_url).

TraceStore() uses the database's default schema: production, and the test database for the stack.
TraceStore(path) uses a schema of its own, derived from `path`: tests pass a fresh tmp_path, so every test
starts with empty tables and never sees another test's rows.

Message content may only be stored as `RedactedText`. Any other string longer than SHORT_LIMIT raises, so
raw chat text cannot be persisted by accident. Short plain strings (reason codes, step names, model ids)
are fine.
"""

import hashlib
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict  # noqa: F401  (re-exported for the check below)
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from chatbot.config import database_url
from chatbot.models import RedactedText

SHORT_LIMIT = 64
HISTORY_HOURS = 48  # how far back the chat history reaches, from the message's own time
RECENT_HOURS = 1  # within this, the history includes every squad; older, only the message's own squad
RETRYABLE = {"received", "enqueue_failed"}  # a delivery that died before reaching the queue may be retried

TABLES = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id   TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    sender_id  TEXT,
    sent_at    TEXT,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    trace_id    TEXT NOT NULL REFERENCES traces(trace_id),
    seq         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    input_json  JSONB,
    output_json JSONB,
    error       TEXT,
    PRIMARY KEY (trace_id, seq)
);
CREATE TABLE IF NOT EXISTS outbox (
    trace_id   TEXT NOT NULL REFERENCES traces(trace_id),
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    to_member  TEXT,
    text       TEXT NOT NULL,
    channel    TEXT,
    status     TEXT NOT NULL,
    error      TEXT,
    squad      TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT,
    PRIMARY KEY (trace_id, seq)
);
CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    sent_at    TIMESTAMPTZ NOT NULL,
    kind       TEXT NOT NULL,            -- member | bot
    squad      TEXT NOT NULL,            -- the squad the message belongs to (for a bot post: the squad it was addressed to)
    speaker    TEXT NOT NULL,            -- roster pseudonym for a member, 'bot' for the bot
    addressee  TEXT,                     -- for a bot post: the member it was addressed to
    source     TEXT NOT NULL,            -- live | replay
    trace_id   TEXT,
    text       TEXT NOT NULL             -- redacted, as everything else in the store
);
CREATE INDEX IF NOT EXISTS messages_squad_time ON messages (squad, sent_at);
"""


class UnredactedTextError(ValueError):
    """A long plain string was passed where only RedactedText may go."""


def check_payload(value, path="payload"):
    """Raise if `value` holds a long string that is not RedactedText."""
    if isinstance(value, RedactedText):
        return
    if isinstance(value, str):
        if len(value) > SHORT_LIMIT:
            raise UnredactedTextError(f"{path}: plain string of {len(value)} chars; wrap it in RedactedText")
    elif isinstance(value, dict):
        for k, v in value.items():
            check_payload(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            check_payload(v, f"{path}[{i}]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


PRODUCTION_DATABASE = "chatbot_prod"


def refuse_if_production(dsn: str) -> None:
    """Destructive actions (clearing the store) are never allowed against the production database."""
    if conninfo_to_dict(dsn).get("dbname") == PRODUCTION_DATABASE:
        raise RuntimeError("refusing to clear the production database")


def _schema_for(path) -> str:
    """A schema name for a store built from a path: stable for the same path, different for different paths."""
    return "t_" + hashlib.sha1(str(path).encode()).hexdigest()[:16]


class Step:
    """Collects a step's input and output. Use via `TraceStore.step`."""

    def __init__(self):
        self.input: dict | None = None
        self.output: dict | None = None


class TraceStore:
    def __init__(self, path=None, dsn: str | None = None):
        self.dsn = dsn or database_url()
        self.schema = "public" if path is None else _schema_for(path)
        if self.schema != "public":
            with psycopg.connect(self.dsn, autocommit=True) as c:
                c.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
        with self._conn() as c:
            # Ingest, the agent and the replay tool start together and all create the tables: take a lock so
            # one creates them and the others wait, then find them already there.
            c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema,))
            c.execute(TABLES)

    @contextmanager
    def _conn(self, write=False):
        """One short transaction per call. Commits on success, rolls back on any exception."""
        conn = psycopg.connect(self.dsn, options=f"-c search_path={self.schema}", row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- traces

    def start_trace(self, trace_id, source, sender_id, sent_at, status="received") -> bool:
        """Create a trace. Returns False if one already exists and was not left in a retryable state (a duplicate delivery)."""
        for v in (trace_id, source, sender_id, sent_at):
            check_payload(v)
        try:
            with self._conn() as c:
                row = c.execute("SELECT status FROM traces WHERE trace_id=%s FOR UPDATE", (trace_id,)).fetchone()
                if row is not None:
                    return row["status"] in RETRYABLE
                c.execute(
                    "INSERT INTO traces(trace_id, source, sender_id, sent_at, status, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                    (trace_id, source, sender_id, sent_at, status, _now()),
                )
                return True
        except errors.UniqueViolation:
            return False  # another process created it between our check and our insert

    def set_status(self, trace_id, status):
        check_payload(status)
        with self._conn() as c:
            c.execute("UPDATE traces SET status=%s WHERE trace_id=%s", (status, trace_id))

    # ---- steps

    @contextmanager
    def step(self, trace_id, name):
        """Time a pipeline stage and record it, with the exception type if it raises."""
        s = Step()
        started = _now()
        t0 = time.monotonic()
        error = None
        try:
            yield s
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self._write_step(trace_id, name, started, int((time.monotonic() - t0) * 1000), s, error)

    def _write_step(self, trace_id, name, started, duration_ms, s, error):
        check_payload(s.input, "input")
        check_payload(s.output, "output")
        with self._conn() as c:
            # Lock the trace row, so two processes writing steps for one trace take turns and numbers never clash.
            c.execute("SELECT 1 FROM traces WHERE trace_id=%s FOR UPDATE", (trace_id,)).fetchone()
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM steps WHERE trace_id=%s", (trace_id,)).fetchone()["next"]
            c.execute(
                "INSERT INTO steps(trace_id, seq, name, started_at, duration_ms, input_json, output_json, error)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    trace_id, seq, name, started, duration_ms,
                    None if s.input is None else Jsonb(s.input),
                    None if s.output is None else Jsonb(s.output),
                    error,
                ),
            )

    # ---- outbox: every reply the agent makes, whichever channel delivers it

    def outbox_begin(self, trace_id, seq, kind, to_member, text, squad=None) -> bool:
        """Record a reply as pending. Returns False if it was already sent or skipped (a redelivered message)."""
        for v in (trace_id, kind, to_member, text):
            check_payload(v)
        with self._conn() as c:
            row = c.execute("SELECT status FROM outbox WHERE trace_id=%s AND seq=%s", (trace_id, seq)).fetchone()
            if row is not None:
                return row["status"] in ("pending", "failed")
            c.execute("INSERT INTO outbox(trace_id, seq, kind, to_member, text, status, squad, created_at)"
                      " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (trace_id, seq, kind, to_member, str(text), "pending", squad, _now()))
            return True

    def outbox_finish(self, trace_id, seq, channel, status, error=""):
        check_payload(channel)
        check_payload(status)
        with self._conn() as c:
            c.execute("UPDATE outbox SET channel=%s, status=%s, error=%s, sent_at=%s WHERE trace_id=%s AND seq=%s",
                      (channel, status, error or None, _now() if status == "sent" else None, trace_id, seq))

    def get_outbox(self, trace_id):
        with self._conn() as c:
            return list(c.execute("SELECT * FROM outbox WHERE trace_id=%s ORDER BY seq", (trace_id,)).fetchall())

    def list_outbox(self, limit=50):
        with self._conn() as c:
            return list(c.execute("SELECT * FROM outbox ORDER BY created_at DESC, seq LIMIT %s", (limit,)).fetchall())

    # ---- chat history: the group's messages, member and bot, kept for context

    def record_message(self, *, kind, sent_at, squad, speaker, text, source, trace_id=None, addressee=None) -> int:
        """Store one message of the chat. The text must already be redacted (RedactedText), never a plain string."""
        if not isinstance(text, RedactedText):
            raise TypeError("chat history stores only RedactedText")
        with self._conn() as c:
            return c.execute(
                "INSERT INTO messages(sent_at, kind, squad, speaker, addressee, source, trace_id, text)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (sent_at, kind, squad, speaker, addressee, source, trace_id, str(text))).fetchone()["id"]

    def list_messages(self, squad=None) -> list[dict]:
        """Stored chat history, oldest first: all squads, or one. For the History tab."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT id, sent_at, kind, squad, speaker, addressee, source, trace_id, text FROM messages"
                " WHERE (%s::text IS NULL OR squad = %s::text) ORDER BY sent_at, id", (squad, squad)).fetchall())

    def update_message(self, message_id, *, kind, sent_at, squad, speaker, addressee, text) -> bool:
        """Replace a stored message's editable fields. Returns False if there is no such message."""
        if not isinstance(text, RedactedText):
            raise TypeError("chat history stores only RedactedText")
        with self._conn() as c:
            return c.execute(
                "UPDATE messages SET kind=%s, sent_at=%s, squad=%s, speaker=%s, addressee=%s, text=%s WHERE id=%s",
                (kind, sent_at, squad, speaker, addressee, str(text), message_id)).rowcount == 1

    def get_message(self, message_id) -> dict | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM messages WHERE id=%s", (message_id,)).fetchone()

    def delete_message(self, message_id) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM messages WHERE id=%s", (message_id,)).rowcount == 1

    def clear_history(self):
        """Empty the chat history only. Refused for production."""
        refuse_if_production(self.dsn)
        with self._conn() as c:
            c.execute("TRUNCATE messages RESTART IDENTITY")

    def history(self, squad, until, hours=HISTORY_HOURS, recent_hours=RECENT_HOURS, exclude_trace=None) -> list[dict]:
        """The context for a message from `squad` at `until` (a message's own time), oldest first.

        Within `recent_hours` before `until`: every squad's messages, since several squads may be part of the
        same conversation. Older, up to `hours`: this squad's messages only.
        The message being answered is left out by its trace id."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT sent_at, kind, squad, speaker, addressee, text, trace_id FROM messages"
                " WHERE sent_at <= %s AND sent_at >= %s - make_interval(hours => %s)"
                " AND (squad = %s OR sent_at >= %s - make_interval(hours => %s))"
                " AND (%s::text IS NULL OR trace_id IS DISTINCT FROM %s::text) ORDER BY sent_at",
                (until, until, hours, squad, until, recent_hours, exclude_trace, exclude_trace)).fetchall())

    def clear(self):
        """Empty every table of this store: traces, steps, outbox and chat history. Refused for production."""
        refuse_if_production(self.dsn)
        with self._conn() as c:
            c.execute("TRUNCATE traces, steps, outbox, messages RESTART IDENTITY CASCADE")

    # ---- reads

    def get_trace(self, trace_id):
        with self._conn() as c:
            t = c.execute("SELECT * FROM traces WHERE trace_id=%s", (trace_id,)).fetchone()
            if t is None:
                return None
            steps = c.execute("SELECT * FROM steps WHERE trace_id=%s ORDER BY seq", (trace_id,)).fetchall()
        return {**t, "steps": [self._decode(r) for r in steps]}

    def list_traces(self, limit=50, include_lab=False):
        """Recent message traces. Prompt-lab calls (source 'lab') are left out unless asked for."""
        with self._conn() as c:
            return list(c.execute("SELECT * FROM traces WHERE (%s OR source <> 'lab') ORDER BY created_at DESC LIMIT %s",
                                  (include_lab, limit)).fetchall())

    def llm_spend(self, trace_id=None, since=None) -> float:
        """Sum of the recorded cost of model calls, for one trace and/or steps started at or after `since` (ISO time)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COALESCE(SUM((output_json->>'cost_usd')::float), 0) AS spent FROM steps"
                " WHERE name LIKE 'llm:%%' AND (%s::text IS NULL OR trace_id = %s::text)"
                " AND (%s::text IS NULL OR started_at >= %s::text)",
                (trace_id, trace_id, since, since)).fetchone()
        return float(row["spent"] or 0)

    @staticmethod
    def _decode(row):
        """The step as the rest of the code expects it: `input` and `output` as Python values."""
        d = dict(row)
        for key in ("input", "output"):
            d[key] = d.pop(f"{key}_json")
        return d
