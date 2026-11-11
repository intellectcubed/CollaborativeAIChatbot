# Scenario agent: a model that plays group members to test the chatbot

Status: design, not built. Builds on [external_client.md](external_client.md), which defines how a client
posts messages and reads replies.

## 1. What it is

A test driver that plays members of the EMS scheduling group chat. It writes messages as a squad member would,
waits for the chatbot's replies, decides what to say next, and keeps going until the scenario's goal is reached
or a limit is hit. Afterwards it checks what happened against the calendar and the chat history.

It is a program on your machine, not a configuration you give to a chat session. The model is called from code
through the Anthropic Messages API with tool use. The tools are ordinary functions in that program that call the
replay tool's HTTP API and read the calendar. Nothing about it runs inside the production stack.

Why code and not a Claude Code session:

| | Program (recommended) | Interactive Claude Code session |
|---|---|---|
| Repeatable | Yes: same scenario file, same settings, saved transcript | No: depends on the session |
| Runs unattended, many scenarios in one go | Yes | No |
| Budget and turn limits enforced in code | Yes | Only by instruction |
| Exploratory, one-off poking around | Possible but heavier | Good |

A useful split: build the program for the scenario suite, and point Claude Code at the same tools (section 8) when
you want to explore by hand.

## 2. How a run works

```
scenario file ──► scenario agent (program)
                    │  system prompt (role + tools + rules)        ← prompts/scenario_agent/system.md
                    │  scenario prompt (goal, personas, window)    ← test_cases/agent_scenarios/<name>.toml
                    │
                    ├── model call (Messages API, tool use, temperature, max tokens)
                    │       │
                    │       ▼ the model asks for a tool, e.g. post_message(...)
                    ├── tool runs in code: checks rules, calls replay /api/send, waits for the trace
                    │       │
                    │       ▼ result goes back to the model
                    └── loop until: goal reached, turn limit, budget, or the model says stop
                              │
                              ▼
                    transcript + report (saved, not posted anywhere)
```

Each iteration: the model reads the latest reply, decides the next message, calls a tool, reads the result.
The program enforces every limit. The model cannot exceed them by asking.

## 3. What you configure

Two layers, both plain files you edit.

### 3.1 Overall role (one file, rarely changed)

`prompts/scenario_agent/system.md`: the role and rules, the same for every scenario. For example:

- You are testing a chatbot that takes part in an EMS squad scheduling group chat.
- You can write messages as members of any squad, using the tools. Write as a real member would: short, informal,
  sometimes ambiguous, sometimes with a typo.
- You can check the calendar and the chat history with tools. Use them to judge results. Do not change the calendar
  except through the post tool.
- Stay inside the date window you are given. Never post outside it.
- End the run with `finish`, stating whether the goal was met and why.

This is a prompt file, like the chatbot's own prompts, so it can be edited and versioned the same way.

### 3.2 Per scenario (one file per scenario)

`test_cases/agent_scenarios/<name>.toml`. Example for the "several turns" case:

```toml
name = "no_crew_clarify_loop"
goal = """A squad that is scheduled says it cannot cover tonight. The chatbot should ask which hours.
Answer the first question with partial information, so it asks again. Then give the full hours. Finish when the
calendar shows the squad's crew removed for those hours, or when the chatbot stops asking."""

window = { start = "2026-03-01", end = "2026-03-31" }  # every posted sent_at must fall in this range
personas = [                                            # who may write; the agent picks from these
  { squad = "54", members = ["Member-2", "Member-5"] },
]
initial_history = "sample_squad54"                      # optional: scenario file for the history to load first

[model]
name = "claude-sonnet-5-5"        # chosen model; check it accepts the temperature below
temperature = 0.7                 # 0 = as repeatable as possible, higher = more varied wording
max_tokens = 1024

[limits]
max_messages = 12                 # posts to the chatbot in one run
max_turns = 30                    # model calls in one run
reply_timeout_s = 120             # how long to wait for the chatbot before going on
max_cost_usd = 0.50               # stops the run when the model spend passes this

[calendar]
live = false                      # false: preview only. true: writes to the calendar (see section 6)
```

Things you can tweak per scenario:

- **Goal**: plain language, with a stop condition. This is what makes "several turns of questions" testable.
- **Personas**: which squads and members the agent may impersonate. A two-squad conversation lists two squads.
  The agent may alternate between them.
- **Window**: the month (or any range). Posts outside it are refused by code.
- **Starting history**: reuse a batch scenario's history, so the chatbot already has context.
- **Model, temperature, max tokens**: how varied and how long the agent's messages are.
- **Limits**: messages, turns, time to wait for replies, money.
- **Calendar**: preview or live. Default preview.

## 4. The dates: how a March request is posted

The agent is told the window, and it chooses each `sent_at`. The tool then checks it.

Example: a request for Saturday, March 14 is posted with `sent_at` on Tuesday, March 10 at 18:00, so the chatbot
reads "Saturday" as the 14th. The message text says "Saturday" or "the 14th", as a member would.

Rules the tool enforces:

1. `sent_at` must be inside the window. Refused otherwise.
2. `sent_at` must be a New York time, written with its offset.
3. Optional check (recommended): if the message names a date, the tool checks that `sent_at` is no later than that
   date and no more than N days before it. This catches a mistake where the agent posts a request after the day it
   is about. It only warns, it does not block, because some wording is legitimately about the past.

The agent is told this in its system prompt. The post tool's description says it again, so the model sees the rule
at the point of use.

## 5. Tools the model can call

Each tool is a function in the program. Results are short and plain.

| Tool | Does | Notes |
|---|---|---|
| `list_members` | Roster pseudonyms and squads the agent may use | Filtered to the scenario's personas. No real names. |
| `post_message(member, text, sent_at)` | Posts through `/api/send` as that member | Enforces window, persona and live flag. Returns the trace id. |
| `wait_for_reply(trace_id)` | Polls the trace until final, then returns the outbox | Per the external client rules: 1 s poll, timeout from config. |
| `get_history(squad, until)` | The chat history the chatbot would see | Useful to check what the chatbot had. |
| `get_calendar_day(date)` | Calendar contents for one date | Read only. |
| `get_trace(trace_id)` | The chatbot's steps for one message | For judging why it answered as it did. |
| `finish(passed, reason)` | Ends the run and records the verdict | Required last call. |

The agent cannot: edit the calendar except through `post_message` with live on, change the policy or config, read
real names, or post to GroupMe. It only ever talks to the replay tool.

## 6. Safety and where the writes go

- Default is preview only. A live run must be chosen in the scenario file and shown in the run's header.
- Live calendar writes also need the stack's own switches (`CALENDAR_ALLOW_WRITES` and the policy mode), as they
  do for the replay tool today. Nothing new is unlocked.
- Test database only. The agent cannot be pointed at production; the replay tool refuses to clear production, and
  the same settings apply.
- Hard limits in code: messages, turns, cost. A run that hits a limit stops and says so.
- The model never sees real names or sender IDs. It gets roster pseudonyms, as the chatbot does.
- Transcripts are written to a local folder, and they are not committed.

## 7. Output

Per run, in a folder named by scenario and time:

- `transcript.jsonl`: every message posted, every reply, every tool call and result, with model token counts and cost.
- `report.md`: the verdict from `finish`, the messages, the chatbot's replies, the calendar before and after, and
  any tool refusals (window, persona, limits).
- `summary.json`: scenario name, model, temperature, counts, cost, final status.

A batch of scenarios writes one summary table across runs, so you can compare two prompt versions the same way you
compare labeled examples.

## 8. Where it runs, and the alternative

- The program runs on your machine, with the replay tool and the calendar that the stack already uses.
- Entry point: a command such as `python -m chatbot.tester run test_cases/agent_scenarios/no_crew_clarify_loop.toml`,
  with a `--all` option for the suite. It is a separate package from the chatbot's own code.
- Its model calls use their own budget and kill switch, separate from the chatbot's, so testing cannot use up the
  chatbot's budget.
- Alternative for exploration: expose the same tools as an MCP server, and drive them from a Claude Code session
  with you in the loop. Same tools, no automatic runs.

## 9. Open decisions

1. Model for the tester. A stronger model writes more realistic and more varied conversation; a cheaper one is fine
   for scripted cases. Suggest one model for all runs and a separate config entry for it.
2. Verdicts. `finish` lets the model judge. For regression use you probably want deterministic checks as well
   (for example "calendar day X shows squad 54 removed 1800-0600"). Both can coexist: the model's verdict is reported,
   and the deterministic checks decide pass or fail.
3. Live calendar in scenarios. Allow it at all? Suggest it is allowed only for the scenarios you mark, and never
   for the regression suite.
4. How many personas a run may use. Two squads is the minimum for the squad-to-squad case.

## 10. Suggestions

- **Build in stages.** (a) Scripted scenarios: the same tools, with the posts written in the scenario file. No
  model, fully repeatable, good for regression. (b) Add the model for the multi-turn and free conversation
  scenarios. (c) Add the judge for verdicts. Stage (a) also tests the tools.
- **Seed the randomness.** With temperature above 0 runs differ. Store each run's transcript so a result can be
  replayed and inspected, even when it is not repeated exactly.
- **Start with the four you named:** random conversation, no-crew request, add-shift request, and "are we scheduled
  for this Saturday?". The last one is the most useful first check, because the answer is on the calendar and can be
  verified without a model.
- **Cap the first runs tightly**: a few messages, a low cost limit, preview only, until the tools are trusted.
- **Keep scenario files small and named by behaviour**, so a failing run points at one behaviour.
- **Monthly windows** are a good default. Add a "relative to the calendar" option later, for example "every Saturday
  in March", which the code expands into concrete dates.

## 11. Decisions (agreed)

1. **Model is a UI setting.** The tester's model is chosen in the replay tool's UI, from a list fetched from the
   provider, the same way the chatbot's models are chosen. Starting value: `claude-opus-5-5`. The temperature and
   limits are set there too, and saved per scenario.
2. **Live writes are allowed, on the test calendar only.** The tester's calendar tool is pointed at the test
   calendar (port 7007 in the current stack; production is port 8007). The tool refuses any other address, in code,
   not by instruction.
3. **Setup actions.** A scenario can set up the calendar before the conversation starts, so the test has a known
   condition. Examples: remove a crew from a day, then post a no-crew request for that day, expecting the chatbot to
   fail to find a crew to remove; or schedule a crew on a day, then post "you won't have a crew for that day or part
   of it". Setup actions are logged in the transcript, and only the test calendar accepts them.
4. **Build order.** The driver (the program that starts and drives runs) is a separate project. It is set up first.
   Agent code follows once the driver exists.

## 12. What the driver needs to provide (for the separate project)

- Start a run from a scenario file and the settings chosen in the UI (model, temperature, limits, calendar mode).
- Run the setup actions, then the conversation loop, then the verdict.
- Call the replay tool's HTTP API for posting (`POST /api/send`), trace status and replies (the external client rules
  in [external_client.md](external_client.md)), and history.
- Call the calendar only through the test-calendar guard described in decision 2.
- Write the transcript, the report and the summary to a local folder.
- Enforce the window, personas and limits in code, and report any refusals.
