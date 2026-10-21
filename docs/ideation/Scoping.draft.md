# Station 95 Scheduling Agent: Scoping (DRAFT v1)

> Draft sections for review. Your original `Scoping.md` is untouched. Items marked **[CONFIRM]** are my assumptions or placeholders that need your input.
> "Collaborative" in the project folder name refers to Station 95 (the five-squad collaborative); it has no design meaning here.

## Problem

Five volunteer EMS squads share 911 coverage across their territories as Station 95. Coverage changes (crew shortages, swaps, added or cancelled shifts) are announced in a shared GroupMe chat, in free text, mixed in with unrelated conversation. Today a person has to read the chat, decide which messages affect coverage, work out the dates, squads and time blocks involved, check them against the schedule, and edit the Google Sheets duty calendar by hand. **[CONFIRM: who does this today, how often, and how long it takes or how often it slips.]**

The calendar is the source of truth for who is covering which territory. When it lags the chat, it can show coverage that doesn't exist, or hide a gap that someone already announced.

## Why it matters / impact

- **Coverage accuracy.** The calendar matches what the chiefs actually agreed, which matters for a 911 service.
- **Admin load.** It removes repetitive transcription work from volunteers who have limited time.
- **Speed.** Changes appear on the calendar in minutes rather than whenever someone gets to it. **[CONFIRM: baseline lag, from the historical data.]**
- **Real production use.** The Martinsville squad would use it, so correctness and failure behavior matter more than they would in a typical demo.
- **Learning value.** It exercises tiered LLM routing, tool use, human-in-the-loop design, evaluation with scarce ground truth, and a supervised self-improvement loop.

## Proposed Approach

### Flow

```
GroupMe -> Ingest (poll 5-10 min batches) -> PII filter (local) -> Conversation store (TTL 24-48h)
        -> Interpretation tiers -> Authorize + validate -> Calendar API -> Confirmation / audit log
                                        \-> low confidence -> human-in-the-loop follow-up
Audit log + admin corrections -> Self-healing agent -> PR -> reviewer agent -> CI eval gate -> admin approval -> deploy
```

### Components

1. **Ingest.** A scheduled job polls GroupMe in 5-10 minute windows, de-duplicates by message ID, and hands batches to the pipeline. Polling is chosen for simplicity and for the volume (weeks of silence, then short bursts). A webhook callback is the alternative if latency matters more later.
2. **PII filter (local, before any model call).** Pattern-based redaction of phone numbers, addresses, names that don't match the roster, and anything that looks like patient information. The design and its known limits are documented explicitly.
3. **Conversation store.** Key-value, with a TTL of 24-48 hours. It holds open threads, pending follow-up questions and the message-to-decision linkage. DynamoDB is the default if it stays within the free tier. **[CONFIRM against current pricing.]**
4. **Tiered interpretation.** Each tier returns a structured result plus a confidence signal, and escalates when the signal is low.
   - **Tier 0, programmatic:** keyword and pattern rules, roster lookup, and matching replies to open follow-up questions.
   - **Tier 1, small Bedrock model:** classify scheduling vs noise, and extract dates, squads and time blocks.
   - **Tier 2, larger Bedrock model:** ambiguous or multi-part messages, plus the judge step.
5. **Decision steps within a tier:** match to an existing conversation, classify, extract dates (resolved against the message timestamp and the squads' time zone), check the calendar via the query API, determine the request, judge the determination, authorize, then act.
6. **Tool layer (calendar API).** Your existing HTTP service handles queries and modifications. **Authorization and validation are enforced here, in deterministic code and not in the prompt.** Examples: the sender belongs to the squad, the squad is actually scheduled on that date, the time block exists, and cross-squad edits pass the guardrails. **[CONFIRM: guardrail rules, still TBD.]**
7. **Human in the loop.** Below the confidence threshold, the agent asks a follow-up in the chat or by DM, or escalates to the admin. **[CONFIRM: channel, and who may answer.]**
8. **Audit log and undo.** Every decision is recorded with its inputs, tier, confidence, action and result. Any automatic write can be reversed from the log. This is also the data source for evaluation and for the self-healing loop.
9. **Self-healing loop.** An agent reads the audit log and the administrator's manual corrections, diagnoses recurring failures, and proposes a code or prompt fix as a pull request. A separate reviewer agent critiques it. CI runs the regression and evaluation suite as a gate, and **you approve every merge**. Nothing deploys without that approval.
10. **Infrastructure.** AWS with Terraform. Bedrock is the LLM provider. A running portability document describes how each component would change to run locally in Docker (for example, replacing the scheduled job and Lambda with a containerized service and a local store).

### Evaluation and test strategy

- **Historical replay.** Replay the last couple of years of chat messages and compare the agent's decisions against what the calendar actually shows.
- **Shadow mode first.** Run read-only against production for a period and compare its decisions to what humans did, before it is allowed to write.
- **Simulated users.** A standalone agent posts realistic messages to a separate QA GroupMe channel. Outcomes are checked automatically, with a human sampling the results.
- **Graduated autonomy.** Move from shadow mode, to autonomous writes for the simplest low-risk actions, to broader actions, as the measured error rates justify it.

## Key success metrics

Targets stay TBD until the historical replay gives a baseline. These are the metrics I would track.

| Area | Metric | How measured |
|---|---|---|
| Classification | Recall and precision on scheduling vs noise (recall matters most) | Labeled history |
| Extraction | Correct date, squad and time block | Labeled history |
| Outcome | Share of auto-applied changes that match the intended calendar state | Replay, admin corrections |
| Safety | **Wrong-write rate** (tracked separately from missed changes; should be near zero) | Audit log, admin corrections |
| Human load | Share of messages escalated; follow-ups resolved without the admin | Audit log |
| Latency | Message to calendar update | Timestamps |
| Cost and tiering | Share resolved at Tier 0 / 1 / 2; cost per message; monthly spend | Audit log, AWS billing |
| Self-healing | PRs proposed, accepted share, regressions after merge | Repo and CI |

## Key risks / unknowns

| Risk | Why it matters | Mitigation or next step |
|---|---|---|
| Wrong direct write to the live calendar | You chose autonomous writes, and this is a 911 coverage record | Deterministic validation, audit log with undo, shadow mode first, autonomy granted per action type |
| Poorly calibrated confidence | LLM self-reported confidence is often unreliable, and the human-in-the-loop trigger depends on it | Combine signals: agreement between tiers, validation outcome, calendar consistency. Calibrate on the historical set. **Unknown: whether this is good enough.** |
| Thin or noisy ground truth | Little live data, and calendar edits may not map cleanly to messages (the calendar may reflect phone calls, etc.) | Historical replay, simulated QA channel, admin corrections as labels |
| Self-healing agent | Chat text is untrusted input that could reach a code-writing agent (prompt injection), and the reviewer shares the author's blind spots | Give it structured logs rather than raw chat, sandbox it, require a CI evaluation gate and your approval, and keep its scope narrow (for example prompts and rules before core code) |
| Identity and authorization | Display names are easy to spoof or mismatch | Map by GroupMe user ID to the roster; enforce permissions in the tool layer |
| Date and time ambiguity | "Tonight", "Saturday", and shifts like 1800-0600 that cross midnight; DST changes | Resolve against message timestamp and time zone, validate against the calendar, escalate when ambiguous |
| Conflicting or concurrent edits | Two contradictory messages, or a manual sheet edit during processing | Conversation store, last-state check before write, conflict escalation |
| Cross-squad guardrails undefined | A core behavior with no spec yet | Define before building the authorization step |
| Cost vs the free-tier goal | Bedrock is pay-per-use and not part of a free tier as far as I know; low volume should keep it small | **[Verify current pricing]** for Bedrock, DynamoDB, Lambda and the scheduler; set a budget alarm |
| PII leakage | Chat may occasionally contain sensitive detail | Local filtering before any model call; document its limits |
| Failure modes | Bedrock, GroupMe or the calendar API could be down | Fail safe: queue and retry, never guess, alert the admin |
| GroupMe API access | Token handling, rate limits, terms of use | **[Verify]** before committing to polling vs webhook |

## Decisions captured so far

- Autonomous writes, with human-in-the-loop only at low confidence.
- Anyone may edit their own squad's schedule, and may edit other squads' schedules under guardrails (TBD).
- Actions in scope: shortages, cancel/add shifts, swaps, acknowledgments.
- Bedrock only (no local model). Portability is handled by documentation.
- Free-tier-first, with low budget. Real production use by the squad.
- Self-healing is a PR-based loop with reviewer agent and admin approval.

## Open questions

1. Guardrail rules for cross-squad edits.
2. Human-in-the-loop channel (group, DM or both) and who may answer.
3. Confidence-threshold design and how it will be calibrated.
4. Whether the simulated-user agent also covers adversarial and ambiguous messages.
5. What counts as a "correction" signal for the self-healing agent (calendar edits by the admin, explicit feedback, or both).
