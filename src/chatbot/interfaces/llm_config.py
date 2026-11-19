"""config/llm.toml: which model, effort and token limit each component uses, and the budget guardrails.

Read by LLMManager for every call, so a saved change applies to the next message. Validated in full on
every load: a file with any problem is rejected, and the manager keeps the last good config.
"""

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

COMPONENTS = ("intent", "tier1")  # the only names a component may use in the file
SOURCES = ("anthropic", "openrouter")
ANTHROPIC_EFFORTS = ("low", "medium", "high", "max")
# OpenRouter sends these as reasoning effort. Which ones a given model accepts is up to the model.
OPENROUTER_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
# Set by the system for every request; `extra` may not override them.
RESERVED_EXTRA = {"model", "messages", "system", "response_format", "max_tokens", "stream"}
MAX_TOKENS_LIMIT = 8192


class ConfigError(ValueError):
    """The config file is missing something or has a value that cannot be used."""


@dataclass(frozen=True)
class ModelSettings:
    source: str  # anthropic | openrouter
    model: str
    max_tokens: int
    effort: str | None = None  # Anthropic only; OpenRouter models read effort through `extra`
    extra: dict = field(default_factory=dict)  # OpenRouter only: name-value pairs added to the request


@dataclass(frozen=True)
class ComponentConfig:
    settings: ModelSettings
    escalate: ModelSettings | None = None  # used at most once per message, in the low-confidence band


@dataclass(frozen=True)
class Budget:
    per_message_usd: float = 0.05  # spend on one message, across all its calls
    per_day_usd: float = 5.00  # spend in one UTC day, across all messages and tests
    kill_switch: bool = False  # True: refuse every call


@dataclass(frozen=True)
class LlmConfig:
    components: dict[str, ComponentConfig]
    budget: Budget = field(default_factory=Budget)

    def component(self, name: str) -> ComponentConfig | None:
        return self.components.get(name)


def _model_settings(data: dict, where: str) -> ModelSettings:
    source = data.get("source", "anthropic")
    if source not in SOURCES:
        raise ConfigError(f"{where}: source must be one of {', '.join(SOURCES)}")
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"{where}: model is required")
    max_tokens = data.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= MAX_TOKENS_LIMIT:
        raise ConfigError(f"{where}: max_tokens must be a whole number from 1 to {MAX_TOKENS_LIMIT}")
    effort = data.get("effort") or None
    allowed = ANTHROPIC_EFFORTS if source == "anthropic" else OPENROUTER_EFFORTS
    if effort is not None and effort not in allowed:
        raise ConfigError(f"{where}: effort for {source} must be one of {', '.join(allowed)} (or left out)")
    extra = data.get("extra", {})
    if not isinstance(extra, dict):
        raise ConfigError(f"{where}: extra must be a table of name = value pairs")
    if source == "anthropic" and extra:
        raise ConfigError(f"{where}: extra is only for openrouter models")
    clash = sorted(RESERVED_EXTRA & set(extra))
    if clash:
        raise ConfigError(f"{where}: extra cannot set {', '.join(clash)}; the system sets those")
    return ModelSettings(source, model.strip(), max_tokens, effort, dict(extra))


def parse(data: dict) -> LlmConfig:
    unknown = sorted(set(data) - {"components", "budget"})
    if unknown:
        raise ConfigError(f"unknown top-level section: {', '.join(unknown)}")
    components = {}
    for name, body in data.get("components", {}).items():
        if name not in COMPONENTS:
            raise ConfigError(f"unknown component '{name}' (known: {', '.join(COMPONENTS)})")
        if not isinstance(body, dict):
            raise ConfigError(f"components.{name} must be a table")
        escalate = body.get("escalate")
        settings_body = {k: v for k, v in body.items() if k != "escalate"}
        components[name] = ComponentConfig(
            _model_settings(settings_body, f"components.{name}"),
            _model_settings(escalate, f"components.{name}.escalate") if escalate is not None else None,
        )
    budget_body = data.get("budget", {})
    budget = Budget(
        per_message_usd=_money(budget_body, "per_message_usd", 0.05, "budget"),
        per_day_usd=_money(budget_body, "per_day_usd", 5.00, "budget"),
        kill_switch=budget_body.get("kill_switch", False),
    )
    if not isinstance(budget.kill_switch, bool):
        raise ConfigError("budget.kill_switch must be true or false")
    return LlmConfig(components, budget)


def _money(body: dict, key: str, default: float, where: str) -> float:
    value = body.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{where}.{key} must be a number of dollars, zero or more")
    return float(value)


def load(path: Path) -> LlmConfig:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {type(exc).__name__}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(data)


def from_models(model: str, intent_model: str | None, intent_max_tokens: int = 1024,
                max_tokens: int = 2048, effort: str | None = "low") -> LlmConfig:
    """The config the agent used before config/llm.toml existed: one Anthropic model for each component."""
    components = {"tier1": ComponentConfig(ModelSettings("anthropic", model, max_tokens, effort))}
    if intent_model:
        components["intent"] = ComponentConfig(ModelSettings("anthropic", intent_model, intent_max_tokens, effort))
    return LlmConfig(components)


# ---- writing: the replay tool's settings panel saves through these

def to_dict(cfg: LlmConfig) -> dict:
    """The config as the file's own shape, so the panel can read it, edit it and send it back."""
    components = {}
    for name, comp in cfg.components.items():
        body = _settings_dict(comp.settings)
        if comp.escalate is not None:
            body["escalate"] = _settings_dict(comp.escalate)
        components[name] = body
    budget = {"per_message_usd": cfg.budget.per_message_usd, "per_day_usd": cfg.budget.per_day_usd,
              "kill_switch": cfg.budget.kill_switch}
    return {"components": components, "budget": budget}


def _settings_dict(s: ModelSettings) -> dict:
    body = {"source": s.source, "model": s.model, "max_tokens": s.max_tokens}
    if s.effort:
        body["effort"] = s.effort
    if s.extra:
        body["extra"] = dict(s.extra)
    return body


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def dumps(data: dict) -> str:
    """Write the file's shape as TOML. Comments are not kept: the file is rewritten whole when saved."""
    lines = ["# Written by the replay tool's LLM settings panel. Comments in this file are not kept on save.", ""]

    def key(k: str) -> str:
        return k if _BARE_KEY.fullmatch(k) else json.dumps(k)

    def value(v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            return json.dumps(v)
        if isinstance(v, list):
            return "[" + ", ".join(value(x) for x in v) + "]"
        raise ConfigError(f"cannot write a {type(v).__name__} to the config file")

    def emit(path: list[str], table: dict) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict) and v is not None}
        if path and scalars:
            lines.append(f"[{'.'.join(key(p) for p in path)}]")
        for k, v in scalars.items():
            lines.append(f"{key(k)} = {value(v)}")
        if scalars:
            lines.append("")
        for k, v in table.items():
            if isinstance(v, dict):
                emit(path + [k], v)

    emit([], data)
    return "\n".join(lines).rstrip() + "\n"
