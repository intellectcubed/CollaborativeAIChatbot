# LLM Strategies

Goal: knobs to turn, so the system can be tuned for better results, without making the code harder to follow.

Keep it simple. Every component makes the same kind of call: a model, an effort level, a token limit, and a structured JSON answer. Anything beyond that is passed through to the provider unchanged, and the user is responsible for knowing what that model supports.

## Single entry point: LLMManager

Every LLM call in the system goes through `LLMManager`. No other code talks to a provider.

`LLMManager` owns:
- **Routing** to a provider, from the component's `source`.
- **Config lookup** for the component, from `config/llm.toml`.
- **Budget guardrails and kill switch** (see below).
- **Recording** for each call: component, source, model, effort, input and output tokens, cost (including cache counters), latency, and the stop reason.
- **Logging**, and writing the trace step. This replaces `TracedLlm`, so prompt lab calls appear in traces too.

Providers only make the API call:
- **`AnthropicClient`** calls the Anthropic API. Prompt caching lives here.
- **`OpenRouterClient`** calls the OpenRouter chat completions endpoint.

## Component configuration

Each component is configured in `config/llm.toml`, keyed by component name. The component passes its name, and `LLMManager` looks up its settings.

The file is read on every call, so a saved change applies to the next message. It is only written from the replay tool (see Developer mode below).

```toml
# config/llm.toml
[components.intent]
source = "anthropic"
model = "claude-haiku-5-5"
effort = "low"
max_tokens = 1024

[components.tier1]
source = "anthropic"
model = "claude-haiku-5-5"
effort = "high"
max_tokens = 2048

# Optional. Used at most once per message, when tier1's confidence is in the low band (0.5 to 0.9).
# Never sticky: the next message starts again from the settings above.
[components.tier1.escalate]
source = "anthropic"
model = "claude-haiku-5-5"
effort = "high"
```

Start with every component on `source = "anthropic"` and `model = "claude-haiku-5-5"`, then change them one at a time.

### Settings

| Setting | Meaning |
|---|---|
| `source` | `anthropic` or `openrouter` |
| `model` | The model ID for that source. For OpenRouter, the full ID, such as `openai/gpt-4o` |
| `effort` | Passed to Anthropic. For OpenRouter it is ignored unless set through `extra` |
| `max_tokens` | Output limit for the call |
| `extra` | Optional name-value pairs added to the OpenRouter request (see below) |
| `escalate` | Optional. A second call with its own settings, used when confidence is in the low band (0.5 to 0.9) |

### Escalation

- Escalation happens when the component's confidence is in the **low band, 0.5 to 0.9**. Below 0.5 the message is not escalated: the system stops and informs the admin, as the gate already does.
- Confidence of 0.9 or above is not escalated: the change is applied.
- It happens **at most once per message**, and the next message starts from the normal settings.
- The component makes the decision. `LLMManager` only executes the call.
- The trace records both calls and why the second one happened.

### OpenRouter and custom parameters

Selecting `source = "openrouter"` uses the same four settings. Anything else OpenRouter accepts goes in `extra`, as name-value pairs that are added to the request body unchanged.

```toml
[components.tier1]
source = "openrouter"
model = "openai/gpt-4o"
max_tokens = 2048

[components.tier1.extra]
temperature = 0.2
```

Rules for `extra`:
- The user is responsible for knowing what each model supports. The system does not check it.
- `model`, `messages` and `response_format` are set by the system and cannot be overridden from `extra`. A config that tries is rejected.
- Every OpenRouter call asks for a JSON schema response. If a model does not support that, the call fails and the trace records why. Unstructured output is out of scope for now.

## Budget guardrails and kill switch

Cost is controlled in `LLMManager`, so every call is checked.

```toml
# config/llm.toml
[budget]
per_message_usd = 0.05    # a single message may not spend more than this, across all its calls
per_day_usd = 5.00        # all calls in one day (UTC)
kill_switch = false       # true: every call is refused until set back to false
```

- **Per-message cap:** checked before each call. If the next call would exceed it, the call is refused and the message ends with an error in its trace.
- **Per-day cap:** the day's spend is summed from the trace store, not kept in memory, so it survives restarts and container rebuilds.
- **Kill switch:** when `kill_switch = true`, every call is refused immediately. It can be set from the replay tool's configuration panel, or by editing the file on the developer's machine.
- A refused call is never silent: it is logged and appears in the trace and the replay tool's banner.

## Developer mode: the replay tool

Two modes:
- **Live mode:** the webhook receives messages from the group chat and updates the live calendar. Nothing here can change `config/llm.toml`, `config/policy.toml` or any prompt file.
- **Developer mode:** the replay tool is used by the system administrator to simulate chats, try prompts, and tune settings. This is the only place where config is written.

### LLM configuration panel

A panel in the replay tool edits one component's settings. Each save writes `config/llm.toml`, and the change applies to the next live message.

From the panel you can:
- Choose the **source**: Claude account (Anthropic) or OpenRouter.
- Choose the **model**:
  - For Anthropic, from the known list.
  - For OpenRouter, from the full list of models, with type-to-filter by name, or by scrolling.
- Set **effort** and **max_tokens**.
- Add, edit or remove **extra** name-value pairs, when the source is OpenRouter.
- Set the **escalate** model and effort, or turn escalation off.
- **Try** the settings on one trace without saving, the same way the prompt editor works.
- **Save** to `config/llm.toml`, with a confirmation, because it changes live behaviour.
- Set the **budget** values and the **kill switch**.

The panel shows the saved config and what it will change. Saving is atomic, the same as prompt saves.

## Secrets

- `ANTHROPIC_API_KEY` and `OPENROUTER_API_KEY` go in `.env`, read only by the agent container, as the Anthropic key is today.
- The replay container calls the agent for model work, so it does not need either key.

## Privacy

Only redacted text is sent to a model, as before. OpenRouter forwards requests to the model's provider, which is a further party. Before enabling a provider through OpenRouter, check its data-retention and training settings, and restrict OpenRouter's routing to providers you have reviewed.

## Phasing

1. **Manager, no behaviour change.** Route all calls through `LLMManager`, with `AnthropicClient` as its only provider. Traces, cost and output stay the same. The existing tests must pass unchanged, and the prompt lab appears in traces.
2. **Config file.** Components read `config/llm.toml`. Start with the same model everywhere. An invalid file stops startup, and a bad edit during operation keeps the last good config and logs an error.
3. **Budget and kill switch.** Per-message and per-day caps, and the kill switch.
4. **Escalation.**
5. **OpenRouter.** `OpenRouterClient`, the `extra` pass-through, and the reserved-key check.
6. **Configuration panel.**

## Acceptance checks

- Traces match the pre-refactor output for the same messages.
- Recorded cost equals the provider's reported usage.
- An unknown component name stops startup.
- A call above the per-message cap is refused, and the trace says why.
- With the kill switch on, no provider is called.
- An `extra` entry that tries to set `model` or `messages` is rejected.
- Escalation happens at most once per message, and the next message uses the normal settings.
