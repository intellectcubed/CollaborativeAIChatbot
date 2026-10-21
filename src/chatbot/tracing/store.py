"""SQLite trace store shared by ingest, the agent and the replay tool.

Several processes write to one file, so every call opens its own short-lived connection, uses WAL
mode and a busy timeout, and takes the write lock with BEGIN IMMEDIATE.

Message content may only be stored as `RedactedText`. Any other string longer than SHORT_LIMIT
raises, so raw chat text cannot be persisted by accident. Short plain strings (reason codes,
step names, model ids) are fine.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from chatbot.models import RedactedText

SHORT_LIMIT = 64
RETRYABLE = {"received", "enqueue_failed"}  # a delivery that died before reaching the queue may be retried

SCHEMA = """
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
    input_json  TEXT,
    output_json TEXT,
    error       TEXT,
    PRIMARY KEY (trace_id, seq)
);
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


class Step:
    """Collects a step's input and output. Use via `TraceStore.step`."""

    def __init__(self):
        self.input: dict | None = None
        self.output: dict | None = None


class TraceStore:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.execute("COMMIT")
        except BaseException:
            if write and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ---- traces

    def start_trace(self, trace_id, source, sender_id, sent_at, status="received") -> bool:
        """Create a trace. Returns False if one already exists and was not left in a retryable state (a duplicate delivery)."""
        for v in (trace_id, source, sender_id, sent_at):
            check_payload(v)
        with self._conn(write=True) as c:
            row = c.execute("SELECT status FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            if row is not None:
                return row["status"] in RETRYABLE
            c.execute(
                "INSERT INTO traces(trace_id, source, sender_id, sent_at, status, created_at) VALUES (?,?,?,?,?,?)",
                (trace_id, source, sender_id, sent_at, status, _now()),
            )
            return True

    def set_status(self, trace_id, status):
        check_payload(status)
        with self._conn(write=True) as c:
            c.execute("UPDATE traces SET status=? WHERE trace_id=?", (status, trace_id))

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
        with self._conn(write=True) as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM steps WHERE trace_id=?", (trace_id,)).fetchone()[0]
            c.execute(
                "INSERT INTO steps(trace_id, seq, name, started_at, duration_ms, input_json, output_json, error)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    trace_id, seq, name, started, duration_ms,
                    None if s.input is None else json.dumps(s.input),
                    None if s.output is None else json.dumps(s.output),
                    error,
                ),
            )

    # ---- reads

    def get_trace(self, trace_id):
        with self._conn() as c:
            t = c.execute("SELECT * FROM traces WHERE trace_id=?", (trace_id,)).fetchone()
            if t is None:
                return None
            steps = c.execute("SELECT * FROM steps WHERE trace_id=? ORDER BY seq", (trace_id,)).fetchall()
        return {**dict(t), "steps": [self._decode(r) for r in steps]}

    def list_traces(self, limit=50):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM traces ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _decode(row):
        d = dict(row)
        for key in ("input", "output"):
            raw = d.pop(f"{key}_json")
            d[key] = None if raw is None else json.loads(raw)
        return d
