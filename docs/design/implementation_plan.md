# Implementation plan: local MVP

Design: [local_mvp.md](local_mvp.md). Scope and constraints: [Scoping.md](../ideation/Scoping.md).

The plan is ordered so that something runs end to end as early as possible, and so the PII filter exists before any model call is wired in. Phases are sized by effort. The 3-week window in Scoping.md started 2026-10-04 with a target of about 2026-10-25, so the phases below are the MVP slice and should be cut rather than extended. If time runs short, the order of what to drop is: Tier 2 escalation, file-mode replay polish, clarification loop, the budget guard.

## Repository layout (target)

```
src/chatbot/
  models.py           GroupMe payload, internal Event, RedactedText
  pii/                filter (patterns, allowlist, Presidio later), reason codes
  tracing/            trace store library (SQLite), step() context manager
  ingest/             FastAPI app: /webhook, queue publisher
  agent/              worker loop, pipeline stages, prompts
  interfaces/         LlmClient, Calendar, ConversationStore, Clock + fakes
  replay/             FastAPI app + static page
config/               roster, squads, allowlist (no real chat text)
tests/                unit, pipeline, PII leak tests
docker/               Dockerfile, compose.yml
```

## Phase 0: skeleton (small)

- Package layout, `pyproject.toml` or extended `requirements.txt`, lint and test setup.
- `docker compose` with ElasticMQ and empty services that start and log.
- Roster and allowlist config files (members as `Member-N` pseudonyms, squads, aliases).

Done when: `docker compose up` starts all containers.

## Phase 1: trace store and models

- `models.py`: GroupMe callback model, internal `Event`, `RedactedText` (constructible only by the PII filter).
- `tracing` library: schema, WAL, `trace.step()` context manager, read helpers (`get_trace`, `list_traces`).
- Tests: concurrent writers from two processes, plus the guard test (plain strings rejected).

Done when: two processes can write steps to one trace and a reader sees them in order.

## Phase 2: PII filter (before any model call)

- Pattern rules, the entity allowlist, and the incident vocabulary list. Presidio and spaCy can come after the first working version.
- Decision outcomes: `pass`, `redacted`, `dropped(reason)`, `uncertain`. Anything other than `pass` or `redacted` is not enqueued.
- Seeded synthetic PII test set and a CI test that fails on any leak.

Done when: the leak test passes and the filter fails closed on errors.

## Phase 3: ingest and queue

- `/webhook` route: validate, assign `trace_id`, filter, enqueue, trace steps `inbound`, `pii_filter`, `enqueue`.
- SQS publisher using `boto3` against ElasticMQ, with a DLQ.
- Test: post a payload and see the redacted event on the queue and the trace rows, with no raw text anywhere.

Done when: a curl POST produces a queued redacted event and a trace.

## Phase 4: agent worker, Tier 0 only

- Worker loop with long polling, delete-after-terminal-state, retry and DLQ.
- Stages: `roster`, `reply_match`, `prefilter`, `date_parse` (rule-based, against `sent_at`).
- `Clock` and the date resolution tests (overnight shifts, "tonight", "this Tuesday", week boundaries).
- Mock `Calendar` and SQLite `ConversationStore`.

Done when: a clear-cut message produces a calendar change from rules alone, and noise is ignored.

## Phase 5: LLM tiers and judge

- `LlmClient` with a Bedrock Converse implementation and a stub.
- Tier 1 (classify and extract JSON), `schedule_lookup`, `judge` with a confidence score, and the confidence gate in `action`.
- Per-call token and cost recording in trace outputs. In-process daily and monthly cost cap that refuses calls when exceeded (the kill switch's app-level half).
- Tier 2 escalation if time allows.

Done when: a replayed labeled example gets an extraction, a judge score and an action, all visible in its trace.

## Phase 6: replay tool

- FastAPI app with a static page, no frontend build.
- Chat mode (sender, date and time, text), file mode (export and labeled examples, all at once or stepping), trace view, mock calendar panel with reset.
- Replay runs set `source=replay` and target the mock calendar.

Done when: you can impersonate a member on a past date and watch the message go through, from the browser.

## Phase 7: clarification loop and confirmations

- Clarify and resume: store a pending conversation with a TTL, match replies in `reply_match`, resume with added context.
- Confirmation and clarification text recorded in the `reply` step. It is posted to GroupMe only once real GroupMe is connected.

Done when: an ambiguous message produces a question, and the reply resolves it in the same trace chain.

## Phase 8: evaluation harness

- Run `test_cases/labeled_examples.csv` through the pipeline (with the context messages first) and compare against the expected action, squads, date and times.
- Report the Scoping.md metrics that apply: classification recall, action accuracy, wrong writes at high confidence, tier distribution, cost per message.

Done when: one command prints a scorecard.

## Later (not in the MVP)

- Real GroupMe connection (webhook plus tunnel, or the poller), posting replies.
- Calendar MCP server.
- Terraform, Lambda, DynamoDB and the AWS Budgets kill switch.
- Self-healing and reviewer agents.
- Arize Phoenix export from the `tracing` spans.

## Risks specific to this plan

- **SQLite shared across containers.** Mitigation: named volume, WAL, and the `/trace` endpoint fallback described in the design.
- **PII filter recall.** Mitigation: allowlist, fail closed, leak test, and replay of the scrubbed history before trusting it.
- **Bedrock access and quotas.** Request model access and check quotas in phase 0. Develop against the stub until access works.
- **Time.** See the cut order at the top.

## Decisions already made

- Webhook target is a FastAPI app; the same route serves live and replay.
- SQS API, with ElasticMQ locally.
- Trace store is a custom SQLite library with several writers and one reader (replay UI).
- The PII filter runs in ingest, before the queue. No raw text is queued, traced or logged.
- Relative dates resolve against the message's `created_at`, never the wall clock.
