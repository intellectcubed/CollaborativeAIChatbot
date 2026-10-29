#!/usr/bin/env python3
"""Find representative scheduling messages in a GroupMe history export.

Pipeline (cheapest steps first, LLM last):
    1. load      read the .jsonl export, drop system/empty/image-only messages
    2. redact    scrub phones, emails, URLs, addresses, @mentions and member names;
                 drop messages that look like incident chatter entirely
    3. filter    keep messages that look schedule-related (keywords, times, squads)
    4. dedupe    collapse near-identical messages, remembering how often each occurred
    5. classify  send REDACTED text in batches to a small LLM (skipped with --dry-run)
    6. select    pick a diverse set of examples per message type

Nothing leaves this machine until step 5, and step 5 only ever sees redacted text.
Always run with --dry-run first and read data/redacted_candidates.jsonl.

Usage:
    python scripts/extract_schedule_examples.py data/groupme_<id>.jsonl --dry-run
    export ANTHROPIC_API_KEY=...
    python scripts/extract_schedule_examples.py data/groupme_<id>.jsonl

Regenerate the markdown after hand-editing the JSON master:
    python scripts/extract_schedule_examples.py --render-only   # also writes schedule_examples.csv

Outputs (in --out-dir, default data/):
    redacted_candidates.jsonl   what would be / was sent to the LLM
    schedule_examples.json      master file: selected examples with all metadata
    schedule_examples.md        markdown view of the same examples for review
    schedule_examples.csv       spreadsheet view (--render-only); asks before overwriting
"""

import argparse
import csv
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 40
CONTEXT_MESSAGES = 2
SQUADS = {"34", "35", "42", "43", "54"}
TYPES = ["no_crew", "add_crew", "swap", "cancel_reversal", "late_early", "question", "other_schedule", "not_schedule"]

# ---------------------------------------------------------------- redaction

PHONE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
URL = re.compile(r"https?://\S+|www\.\S+")
ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Za-z0-9.]+\s+){1,3}"
    r"(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|blvd|boulevard|ct|court|way|pl|place|ter|terrace|pkwy|hwy|highway)\b\.?",
    re.IGNORECASE,
)
MENTION = re.compile(r"@\w+(?:\s[A-Z]\w+)?")

# Messages matching any of these are dropped entirely: they look like incident chatter.
INCIDENT = re.compile(
    r"\bpatient|\bpt\b|\by/?o\b|\byom\b|\byof\b|\byr old|\bmale\b|\bfemale\b|chest pain|cardiac|arrest|overdose|"
    r"narcan|unresponsive|seizure|stroke|trauma|dispatch|respond|transport|hospital|\bCPR\b|\bDOA\b|\bDOB\b|"
    r"\bbleeding|\bfall\b|\bfell\b|\bunconscious|\bbreathing",
    re.IGNORECASE,
)

# A message is a candidate if it matches any of these.
SCHEDULE_HINT = re.compile(
    r"\bcrew\b|\bshift\b|\bcover|\bswap|\btrade\b|\btango\b|\bschedul|\bon duty\b|\boff duty\b|\bpick(?:ing)? up\b|"
    r"\badd(?:ed|ing)? (?:me|us|\d)|\bdrop|\bno one\b|\bnobody\b|\btonight\b|\btomorrow\b|\bthis (?:sat|sun|mon|tue|wed|thu|fri)|"
    r"\b(?:mon|tues?|wed(?:nes)?|thu(?:rs)?|fri|sat(?:ur)?|sun)(?:day)?\b|\b(?:[01]\d|2[0-3])[0-5]\d\b|\b\d{1,2}\s?(?:am|pm)\b|"
    r"\b(?:34|35|42|43|54)\b|green knoll|finderne|manville|martinsville|somerville|\bcrews?\b|\bambulance\b|\btruck\b",
    re.IGNORECASE,
)


def build_name_scrubber(rows):
    """Regex that matches any display-name token (>=3 chars) seen in the export."""
    tokens = set()
    for r in rows:
        for part in re.split(r"[\s.,_-]+", r.get("name") or ""):
            if len(part) >= 3 and part.lower() not in {"the", "and", "squad"} and not part.isdigit():
                tokens.add(re.escape(part))
    return re.compile(r"\b(?:" + "|".join(sorted(tokens, key=len, reverse=True)) + r")\b", re.IGNORECASE) if tokens else None


def redact(text, name_scrubber):
    text = URL.sub("[link]", text)
    text = EMAIL.sub("[email]", text)
    text = PHONE.sub("[phone]", text)
    text = ADDRESS.sub("[address]", text)
    text = MENTION.sub("@member", text)
    if name_scrubber:
        text = name_scrubber.sub("[member]", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


# ---------------------------------------------------------------- pipeline


def load_messages(path):
    rows = [json.loads(line) for line in Path(path).open(encoding="utf-8")]
    rows = [r for r in rows if not r.get("system") and (r.get("text") or "").strip()]
    rows.sort(key=lambda r: r["created_at"])
    return rows


def prepare_candidates(rows):
    """Redact, filter and dedupe. Returns (candidates, stats)."""
    scrubber = build_name_scrubber(rows)
    senders = {}
    clean = []  # one entry per message, in order; text None when dropped as incident chatter
    for r in rows:
        text = r["text"]
        pseudo = senders.setdefault(r["sender_id"], f"Member-{len(senders) + 1}")
        clean.append(
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "sender": pseudo,
                "text": None if INCIDENT.search(text) else redact(text, scrubber),
            }
        )

    stats = Counter(total=len(rows), incident_dropped=sum(1 for c in clean if c["text"] is None))
    by_norm = {}
    for i, c in enumerate(clean):
        if c["text"] is None or not SCHEDULE_HINT.search(c["text"]) or len(c["text"]) > 400:
            continue
        key = normalize(c["text"])
        if key in by_norm:
            by_norm[key]["count"] += 1
            continue
        context = [
            f"{clean[j]['sender']}: {clean[j]['text'] if clean[j]['text'] is not None else '[omitted]'}"[:200]
            for j in range(max(0, i - CONTEXT_MESSAGES), i)
        ]
        by_norm[key] = {**c, "count": 1, "context": context}

    candidates = list(by_norm.values())
    stats["candidates_after_filter"] = sum(c["count"] for c in candidates)
    stats["candidates_after_dedupe"] = len(candidates)
    return candidates, stats


SYSTEM_PROMPT = f"""You help analyze a group chat for a volunteer ambulance collaborative. Five squads (34, 35, 42, 43, 54; also called Green Knoll, Finderne, Manville, Martinsville, Somerville) announce staffing changes. Night shifts run 1800-0600 every day; day shifts 0600-1800 on Saturday and Sunday only. A shift is named for the day it starts.

For each numbered message, decide whether it announces or requests a change to the crew schedule. Reply with ONLY a JSON array, one object per message, in this form:
{{"n": <number>, "type": one of {TYPES}, "squads": [squad numbers mentioned or implied, as strings], "time_scope": "whole_shift" | "partial" | "unspecified", "needs_context": true if the message cannot be understood without the earlier messages, "ambiguity": a short phrase if a bot could misread it (vague day, day-vs-night, unclear squad), else null}}
Types: no_crew = a squad will not staff; add_crew = a squad adds/picks up a crew; swap = squads trade shifts; cancel_reversal = takes back an earlier announcement; late_early = crew arrives late or leaves early; question = asks about the schedule without changing it; other_schedule = schedule-related but none of the above; not_schedule = unrelated chatter."""


def classify(candidates, client):
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        lines = []
        for n, c in enumerate(batch, 1):
            when = datetime.fromtimestamp(c["created_at"], EASTERN).strftime("%a %Y-%m-%d %H:%M")
            ctx = (" | earlier: " + " / ".join(c["context"])) if c["context"] else ""
            lines.append(f"{n}. [{when}] {c['sender']}: {c['text']}{ctx}")
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text = resp.content[0].text
        try:
            results = json.loads(text[text.index("[") : text.rindex("]") + 1])
        except ValueError:
            print(f"\nBatch at {start}: could not parse response, skipping.", file=sys.stderr)
            continue
        for item in results:
            n = item.get("n")
            if isinstance(n, int) and 1 <= n <= len(batch):
                batch[n - 1]["label"] = {k: v for k, v in item.items() if k != "n"}
        print(f"\rClassified {min(start + BATCH_SIZE, len(candidates))}/{len(candidates)}", end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)


def interest(c):
    label = c["label"]
    return (3 if label.get("ambiguity") else 0) + (2 if label.get("needs_context") else 0) + (1 if label.get("time_scope") == "partial" else 0)


def words(text):
    return set(normalize(text).split())


def select_examples(candidates, per_type, total, seed=0):
    rng = random.Random(seed)
    by_type = defaultdict(list)
    for c in candidates:
        label = c.get("label")
        if label and label.get("type") in TYPES and label["type"] != "not_schedule":
            by_type[label["type"]].append(c)

    chosen = []
    for items in by_type.values():
        rng.shuffle(items)
        items.sort(key=interest, reverse=True)
        picked = []
        for c in items:
            if len(picked) >= per_type:
                break
            w = words(c["text"])
            if all(len(w & words(p["text"])) / max(1, len(w | words(p["text"]))) < 0.6 for p in picked):
                picked.append(c)
        chosen.extend(picked)
    chosen.sort(key=lambda c: (-interest(c), c["created_at"]))
    return chosen[:total]


def write_outputs(chosen, stats, out_dir):
    records = []
    for i, c in enumerate(chosen, 1):
        label = c["label"]
        records.append(
            {
                "example_id": i,
                "message_id": c["id"],
                "timestamp_et": datetime.fromtimestamp(c["created_at"], EASTERN).isoformat(),
                "sender": c["sender"],
                "text": c["text"],
                "context": c["context"],
                "times_seen": c["count"],
                "type": label.get("type"),
                "squads": label.get("squads", []),
                "time_scope": label.get("time_scope"),
                "needs_context": label.get("needs_context", False),
                "ambiguity": label.get("ambiguity"),
                "expected_action": "",  # fill in by hand
                "notes": "",  # fill in by hand
            }
        )
    master = {"generated": datetime.now(EASTERN).isoformat(), "stats": dict(stats), "examples": records}
    (out_dir / "schedule_examples.json").write_text(json.dumps(master, indent=2, ensure_ascii=False), encoding="utf-8")

    render_markdown(master, out_dir)


def render_markdown(master, out_dir):
    """Write schedule_examples.md from the master JSON structure."""
    stats, records = master["stats"], master["examples"]
    md = ["# Schedule Message Examples", "", "Redacted examples drawn from the GroupMe history. Edit `schedule_examples.json` (the master); regenerate this view from it.", ""]
    md.append(f"Messages scanned: {stats.get('total', '?')}; schedule-like candidates after filtering: {stats.get('candidates_after_dedupe', '?')}.")
    by_type = defaultdict(list)
    for r in records:
        by_type[r["type"]].append(r)
    for t in TYPES:
        if t not in by_type:
            continue
        md += ["", f"## {t.replace('_', ' ').title()}", ""]
        for r in by_type[t]:
            md.append(f"### Example {r['example_id']}")
            md.append(f"> {r['text']}")
            md.append("")
            md.append(f"- **When:** {r['timestamp_et']}  **Sender:** {r['sender']}  **Seen:** {r['times_seen']}×")
            md.append(f"- **Squads:** {', '.join(r['squads']) or '?'}  **Scope:** {r['time_scope']}")
            if r["needs_context"]:
                md.append("- **Needs context:** " + " / ".join(r["context"]))
            if r["ambiguity"]:
                md.append(f"- **Ambiguity:** {r['ambiguity']}")
            md.append(f"- **Expected bot action:** {r.get('expected_action') or '_TBD_'}")
            md.append(f"- **Notes:** {r.get('notes') or '_TBD_'}")
            md.append("")
    (out_dir / "schedule_examples.md").write_text("\n".join(md) + "\n", encoding="utf-8")


CSV_COLUMNS = ["example_id", "message_id", "timestamp_et", "sender", "text", "context", "type", "squads", "time_scope", "ambiguity", "expected_action", "notes"]


def write_csv(master, path):
    """Write the examples to CSV (UTF-8 with BOM so Excel opens it correctly)."""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in master["examples"]:
            row = dict(r)
            row["context"] = " / ".join(r.get("context") or [])
            row["squads"] = ", ".join(r.get("squads") or [])
            writer.writerow({k: ("" if row.get(k) is None else row[k]) for k in CSV_COLUMNS})


def confirm_overwrite(path, assume_yes):
    if not path.exists() or assume_yes:
        return True
    try:
        return input(f"{path} already exists. Overwrite? [y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("history", type=Path, nargs="?", help="GroupMe .jsonl export (not needed with --render-only)")
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent.parent / "data")
    parser.add_argument("--dry-run", action="store_true", help="redact/filter only; make no LLM calls")
    parser.add_argument("--per-type", type=int, default=5, help="max examples per message type")
    parser.add_argument("--total", type=int, default=30, help="max examples overall")
    parser.add_argument("--max-candidates", type=int, help="cap candidates sent to the LLM (random sample)")
    parser.add_argument("--render-only", action="store_true", help="regenerate schedule_examples.md and .csv from schedule_examples.json and exit")
    parser.add_argument("--force", action="store_true", help="overwrite an existing schedule_examples.json (discards hand edits)")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    if args.render_only:
        master = json.loads((args.out_dir / "schedule_examples.json").read_text(encoding="utf-8"))
        render_markdown(master, args.out_dir)
        print(f"Wrote {args.out_dir / 'schedule_examples.md'} from {len(master['examples'])} examples", file=sys.stderr)
        csv_path = args.out_dir / "schedule_examples.csv"
        if confirm_overwrite(csv_path, args.yes):
            write_csv(master, csv_path)
            print(f"Wrote {csv_path}", file=sys.stderr)
        else:
            print("Skipped CSV.", file=sys.stderr)
        return
    if not args.history:
        parser.error("history file is required unless --render-only is used")

    if not args.dry_run and (args.out_dir / "schedule_examples.json").exists() and not args.force:
        sys.exit("schedule_examples.json already exists and may contain hand edits. Move it aside or pass --force.")

    candidates, stats = prepare_candidates(load_messages(args.history))
    if args.max_candidates and len(candidates) > args.max_candidates:
        random.Random(0).shuffle(candidates)
        candidates = candidates[: args.max_candidates]
        candidates.sort(key=lambda c: c["created_at"])

    with (args.out_dir / "redacted_candidates.jsonl").open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    print(f"Messages with text:            {stats['total']}", file=sys.stderr)
    print(f"Dropped as incident chatter:   {stats['incident_dropped']}", file=sys.stderr)
    print(f"Schedule-like after filter:    {stats['candidates_after_filter']}", file=sys.stderr)
    print(f"Unique after dedupe (to send): {len(candidates)}", file=sys.stderr)
    print(f"Wrote {args.out_dir / 'redacted_candidates.jsonl'}", file=sys.stderr)

    if args.dry_run:
        print("Dry run: nothing sent. Read the file above before running without --dry-run.", file=sys.stderr)
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first.")
    import anthropic

    batches = -(-len(candidates) // BATCH_SIZE)
    if not args.yes:
        answer = input(f"Send {len(candidates)} REDACTED messages in {batches} calls to {MODEL}? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("Aborted.")

    classify(candidates, anthropic.Anthropic())
    chosen = select_examples([c for c in candidates if "label" in c], args.per_type, args.total)
    write_outputs(chosen, stats, args.out_dir)
    print(f"Wrote {len(chosen)} examples to {args.out_dir / 'schedule_examples.json'} and .md", file=sys.stderr)


if __name__ == "__main__":
    main()
