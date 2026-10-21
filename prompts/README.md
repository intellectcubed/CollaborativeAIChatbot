# Prompts

Everything the model is told lives here, not in code. The agent re-reads these files for every
message, so an edit takes effect on the next message with no restart.

| File | Purpose |
|---|---|
| `tier1/system.md` | System prompt for the Tier 1 interpretation call |
| `tier1/user.md` | Template for the per-message user turn. Fields: `{message_time}`, `{sender}`, `{message}` |
| `tier1/schema.json` | JSON schema the response must follow (structured output) |

The trace records the exact text sent and a short hash of these three files as the prompt version.

Rules:
- Never put raw chat text, names, or anything from a real 911 call in a prompt or example. Prompts are
  committed to git and are the one thing the self-healing agent is allowed to change (by pull request).
- If you change `schema.json`, update `Command` and `Interpretation` in `src/chatbot/agent/interpret.py`
  to match. The code validates every response against its own models.
