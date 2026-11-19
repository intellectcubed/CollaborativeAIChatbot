"""The recent chat with a squad, written into the prompt as context.

Only the prompt sees the "bot →" lines. They are built here from the stored rows at render time; the
stored text and anything sent to the chat stay plain. The time split is done here, in code, not by the
model: lines from the last hour, from every squad, go in the "most important" section, and this squad's lines
from the rest of the 48-hour window go in a "less important" section, and only the 10 newest of those are kept (the
store makes the squad and window selection). The prompt says so; the model never works out times or deltas.
Only the most recent lines are kept, so a busy chat cannot grow the prompt without limit.
"""

from datetime import datetime

from chatbot.config import GROUP_TZ
from chatbot.tracing.store import RECENT_HOURS

NO_HISTORY = "(none)"
MAX_LINES = 150  # the most recent lines; older ones are dropped and the prompt says how many
MAX_EARLIER = 10  # the newest older-background lines from this squad; the rest are dropped and the prompt says how many


def label(row: dict) -> str:
    """How a line names its speaker: a member (with squad), or the bot writing to a member of a squad."""
    if row["kind"] == "bot":
        to = row.get("addressee") or "a member"
        return f"bot → {to} (squad {row['squad']})"
    return f"{row['speaker']} (squad {row['squad']})"


def _line(row: dict) -> str:
    when = row["sent_at"].astimezone(GROUP_TZ).strftime("%a %H:%M")
    return f"[{when}] {label(row)}: {row['text']}"


def render(rows: list[dict], until: datetime) -> str:
    """The history section for a prompt. `rows` are one squad's messages, oldest first, before `until`."""
    if not rows:
        return NO_HISTORY
    dropped = max(0, len(rows) - MAX_LINES)
    rows = rows[dropped:]
    recent, earlier = [], []
    for r in rows:
        (recent if (until - r["sent_at"]).total_seconds() <= RECENT_HOURS * 3600 else earlier).append(r)
    parts = []
    if dropped:
        parts.append(f"({dropped} older messages not shown)")
    # Newer lines in the first section matter more than older ones; the second section matters least.
    parts.append("Chat history:")
    parts.append("**Most important (last hour, all squads; newest last, and the newest lines matter most):**")
    parts += [_line(r) for r in recent] or ["(none)"]
    if earlier:
        skipped = max(0, len(earlier) - MAX_EARLIER)
        earlier = earlier[skipped:]
        parts.append("Chat history:")
        parts.append("**Less important (this squad, older background):**")
        if skipped:
            parts.append(f"({skipped} older background messages not shown)")
        parts += [_line(r) for r in earlier]
    return "\n".join(parts)


def history_text(store, squad: str | None, sent_at: datetime, trace_id: str | None) -> str:
    """The section for one message: the squad's chat before it, leaving out the message itself."""
    if not squad:
        return NO_HISTORY  # the sender is not on the roster, so there is no squad to read
    return render(store.history(squad, until=sent_at, recent_hours=RECENT_HOURS, exclude_trace=trace_id), sent_at)
