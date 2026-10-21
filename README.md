# Collaborative AI Chatbot

An AI chatbot that reads a GroupMe chat for ambulance crew schedule changes and updates the shared Google Sheets coverage calendar for a collaborative of five volunteer rescue squads.

## The problem

"The Collaborative" (radio name *Station 95*) is five neighboring EMS squads that pool crews to cover nights and weekends for 911 service. Members announce staffing changes in a GroupMe chat, mixed in with unrelated chatter ("54 won't have a crew Wednesday 2200–0100"). One administrator reads the chat and edits the calendar by hand. When that lags or goes wrong, the calendar stops matching real coverage.

## The approach

The bot replaces the manual read-and-transcribe step:

1. Read new GroupMe messages and decide which ones are schedule changes and which are noise.
2. Work out who is asking and what they mean (squad, dates, times, add/remove/swap), using a deterministic first pass, then a cheap model, then a stronger model only when needed.
3. Check the calendar, then apply the change through a calendar service.
4. Confirm in the chat, or ask a clarifying question when unsure.

The administrator can always override or undo. See [docs/ideation/Scoping.md](docs/ideation/Scoping.md) for the full design, risks, and 3-week plan.

## Privacy

Real 911 call details (patient names, addresses, ages, medical conditions) must never reach an LLM or be stored. Chat text is redacted locally before any model call, and real chat exports are never committed (`data/` is git-ignored).

## Documentation

- [Collaborative domain description](docs/collaborative_domain.md): how the squads, territories, shifts, Tango and the calendar work
- [Scoping and design](docs/ideation/Scoping.md): problem, architecture, success metrics, risks
- [Local MVP design](docs/design/local_mvp.md): webhook, queue, agent, replay tool and trace store
- [Implementation plan](docs/design/implementation_plan.md): phased build order for the local MVP
- [Extracting schedule examples](docs/extract_schedule_examples.md)

## Repository layout

```
docs/          domain description, design, script docs
scripts/       example extraction (redact, filter, classify chat messages)
test_cases/    labeled example messages for evaluating the classifier
data/          local only, git-ignored: chat history and derived files
```

## Status

Early design. The domain is documented, a script extracts candidate messages from a GroupMe export, and a set of real example messages is being collected and labeled. The bot itself is not built yet.

## Setup (scripts)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The example-extraction script reads a GroupMe history export (JSON Lines) and needs an `ANTHROPIC_API_KEY` to classify the redacted messages. See [docs/extract_schedule_examples.md](docs/extract_schedule_examples.md).
