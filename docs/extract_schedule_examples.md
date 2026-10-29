# extract_schedule_examples.py

Finds representative scheduling messages in the GroupMe history so they can be used as the example set for the chatbot. The script lives in `scripts/`. It produces a JSON master file that you edit by hand, and a markdown view that is generated from that JSON.

## What it does

The pipeline runs cheap local steps first and calls an LLM only at the end. The LLM only ever sees redacted text.

| Step | What happens | LLM? |
|---|---|---|
| 1. Load | Reads the `.jsonl` export; drops system messages, empty messages, and image-only messages. | No |
| 2. Redact | Scrubs phone numbers, emails, links, street addresses, @mentions, and every display name found in the export. Senders become `Member-1`, `Member-2`, etc. Messages that look like incident chatter (*patient, y/o, chest pain, narcan, transport, hospital*, ...) are dropped entirely. | No |
| 3. Filter | Keeps messages that look schedule-related: day names, times, squad numbers or town names, words like *crew*, *shift*, *swap*, *tango*. | No |
| 4. Dedupe | Collapses identical messages (ignoring case and punctuation) and counts how often each occurred. | No |
| 5. Classify | Sends about 40 redacted messages per call to Claude Haiku, with the two preceding messages as context. Each message gets a type, squads, time scope, a needs-context flag, and an ambiguity note. | Yes |
| 6. Select | Picks up to 5 examples per type (30 overall), favoring ambiguous, partial-shift, and context-dependent messages, and skipping near-duplicates. | No |

Message types: `no_crew`, `add_crew`, `swap`, `cancel_reversal`, `late_early`, `question`, `other_schedule`. (`not_schedule` messages are discarded.)

Redaction is best-effort. It cannot catch everything, such as a name that is not a display name or a landmark that identifies a location. Always read the redacted file before the first real run.

## Files

All outputs go to `data/` (override with `--out-dir`). Keep this folder out of version control.

| File | Purpose |
|---|---|
| `redacted_candidates.jsonl` | Exactly what is sent to the LLM. Review it before a real run. |
| `schedule_examples.json` | **The master.** Selected examples with all metadata. Edit this file. |
| `schedule_examples.md` | A read-only view generated from the JSON. Do not edit it by hand. |
| `schedule_examples.csv` | A spreadsheet view generated from the JSON by `--render-only`. Columns: example_id, message_id, timestamp_et, sender, text, context, type, squads, time_scope, ambiguity, expected_action, notes. |

Each example in the JSON has the original message ID (to trace it back locally), the redacted text, context, type, squads, scope, `needs_context`, `ambiguity`, plus two fields for you to fill in:

- `expected_action`: what the bot should do, for example `Remove 54 from Thursday night 2200-0100`
- `notes`: anything else worth recording

## Setup

```bash
cd CollaborativeAIChatbot
source .venv/bin/activate
export ANTHROPIC_API_KEY=your_key    # only needed for the classify step
```

## Common commands

**1. Dry run.** Redact, filter, and dedupe only. Nothing is sent anywhere. Do this first, then read `data/redacted_candidates.jsonl`.

```bash
python scripts/extract_schedule_examples.py data/groupme_87780377.jsonl --dry-run
```

**2. Small test run.** Classify a random sample of 100 candidates to check that the output looks right.

```bash
python scripts/extract_schedule_examples.py data/groupme_87780377.jsonl --max-candidates 100
```

**3. Full run.** All unique candidates (about 4,400 at last count; roughly $1–2 with Haiku). It asks for confirmation before sending.

```bash
python scripts/extract_schedule_examples.py data/groupme_87780377.jsonl
```

**4. Regenerate the markdown and CSV after editing the JSON.** No LLM call, no API key needed, no history file needed.

```bash
python scripts/extract_schedule_examples.py --render-only
```

This reads `data/schedule_examples.json` and rewrites `data/schedule_examples.md` and `data/schedule_examples.csv`. Filled-in `expected_action` and `notes` values appear in both; empty ones show as _TBD_ in the markdown and blank in the CSV.

If `schedule_examples.csv` already exists, the script asks `Overwrite? [y/N]` before replacing it. Answer `n` (or press Enter) to keep the existing CSV; the markdown is still regenerated. Pass `--yes` to skip the question. The CSV is saved with a BOM so Excel and Numbers open it with the correct characters.

## Typical workflow

1. Run the dry run and review the redacted candidates.
2. Run a full run (or a small test run first). This creates the JSON and markdown.
3. Open `schedule_examples.json` and fill in `expected_action` and `notes` for each example. You can also delete weak examples or edit text (for example, to remove anything the redaction missed).
4. Run `--render-only` to refresh the markdown and CSV.
5. Repeat steps 3 and 4 as often as you like.

## Options

| Option | Meaning |
|---|---|
| `--dry-run` | Redact and filter only; make no LLM calls. |
| `--render-only` | Regenerate the markdown from the JSON and exit. |
| `--max-candidates N` | Send only a random sample of N candidates to the LLM. |
| `--per-type N` | Max examples per message type (default 5). |
| `--total N` | Max examples overall (default 30). |
| `--out-dir PATH` | Where to read and write outputs (default `data/`). |
| `--yes` | Skip confirmation prompts (the LLM send prompt, and the CSV overwrite prompt with `--render-only`). |
| `--force` | Overwrite an existing `schedule_examples.json`. **This discards your hand edits.** |

## Warning: do not lose your edits

A normal run writes a new `schedule_examples.json`. To protect hand edits, the script refuses to run if that file already exists, unless you pass `--force`. To keep your work and generate a fresh set, move the existing file aside first:

```bash
mv data/schedule_examples.json data/schedule_examples.edited.json
```

## Privacy

The history contains real names and message text from a 911 service. The redacted candidates and the examples are the only data that reach the LLM, and they should never contain incident or caller details. If you find any in `redacted_candidates.jsonl` or the examples, remove them and add the pattern to the incident or redaction lists at the top of the script before running again.
