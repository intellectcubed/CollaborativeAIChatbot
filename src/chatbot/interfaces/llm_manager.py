"""LLMManager: the one place every model call goes through.

Components ask for a call by name ("intent", "tier1"). The manager looks up that component's model,
effort and token limit in config/llm.toml, checks the budget, calls the provider, and records the call:
a trace step with the exact text sent, the response, tokens, cost (including cache counters), latency
and the stop reason, plus a log line. Providers only make the API call.

Budget guardrails are checked before every call, so a refused call still shows in the trace with the
reason. The per-day spend is summed from the trace store, so it survives restarts.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from chatbot.interfaces.llm import LlmClient, LlmError, LlmResult
from chatbot.interfaces.llm_config import ConfigError, LlmConfig, ModelSettings, load
from chatbot.models import RedactedText
from chatbot.prompts import Prompt
from chatbot.tracing.store import TraceStore

log = logging.getLogger("llm")


class BudgetExceeded(LlmError):
    """A budget cap or the kill switch refused the call. Not transient: retrying will not help."""


class LLMManager:
    def __init__(self, providers: dict[str, LlmClient], store: TraceStore, config: LlmConfig | Path):
        self.providers = providers
        self.store = store
        self._path: Path | None = None
        self._mtime: float | None = None
        if isinstance(config, LlmConfig):
            self._config = config
        else:
            self._path = Path(config)
            self._config = load(self._path)  # a bad file stops startup
            self._mtime = self._path.stat().st_mtime
        self._check_providers(self._config)

    # ---- config

    def config(self) -> LlmConfig:
        """The current config. A changed file is re-read; if the new one is invalid the last good one is kept."""
        if self._path is None:
            return self._config
        try:
            mtime = self._path.stat().st_mtime
            if mtime != self._mtime:
                new = load(self._path)
                self._check_providers(new)
                self._config, self._mtime = new, mtime
                log.info("llm config reloaded from %s", self._path)
        except (OSError, ConfigError) as exc:
            log.error("llm config not reloaded, keeping the last good one: %s", exc)
        return self._config

    def _check_providers(self, config: LlmConfig) -> None:
        for name, comp in config.components.items():
            for settings in (comp.settings, comp.escalate):
                if settings is not None and settings.source not in self.providers:
                    raise ConfigError(f"components.{name} uses {settings.source}, which is not available (is its API key set?)")

    def has(self, component: str) -> bool:
        return self.config().component(component) is not None

    def has_escalation(self, component: str) -> bool:
        comp = self.config().component(component)
        return comp is not None and comp.escalate is not None

    # ---- calls

    def bind(self, trace_id: str) -> "BoundLlm":
        return BoundLlm(self, trace_id)

    def complete(self, component: str, prompt: Prompt, user: RedactedText, *, trace_id: str,
                 escalate: bool = False) -> LlmResult:
        comp = self.config().component(component)
        if comp is None:
            raise ConfigError(f"no config for component '{component}' in llm.toml")
        settings = comp.escalate if escalate else comp.settings
        if settings is None:
            raise ConfigError(f"component '{component}' has no escalate settings")
        step = f"llm:{component}" + ("+escalate" if escalate else "")
        return self.call(settings, step_name=step, trace_id=trace_id, system=prompt.system, user=user,
                         schema=prompt.schema, prompt_version=prompt.version)

    def call(self, settings: ModelSettings, *, step_name: str, trace_id: str, system: str, user: RedactedText,
             schema: dict, prompt_version: str | None = None) -> LlmResult:
        """One recorded call with explicit settings. Components use complete(); the prompt lab uses this."""
        budget = self.config().budget
        with self.store.step(trace_id, step_name) as s:
            s.input = {"source": settings.source, "model": settings.model, "effort": settings.effort,
                       "max_tokens": settings.max_tokens, "extra_keys": sorted(settings.extra),
                       "prompt_version": prompt_version, "system": RedactedText.mint(system), "user": user}
            self._check_budget(budget, trace_id)
            t0 = time.monotonic()
            result = self.providers[settings.source].complete(
                system=system, user=user, schema=schema, model=settings.model, max_tokens=settings.max_tokens,
                effort=settings.effort, extra=settings.extra or None)
            latency = int((time.monotonic() - t0) * 1000)
            s.output = {"response": RedactedText.mint(result.text), "stop_reason": result.stop_reason,
                        "latency_ms": latency, "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                        "cache_read_tokens": result.cache_read_tokens, "cache_write_tokens": result.cache_write_tokens,
                        "cost_usd": result.cost_usd, "request_id": result.request_id}
        log.info("llm step=%s source=%s model=%s effort=%s tokens=%d/%d cache=%d/%d cost=%s latency=%dms trace=%s",
                 step_name, settings.source, settings.model, settings.effort, result.input_tokens, result.output_tokens,
                 result.cache_read_tokens, result.cache_write_tokens, result.cost_usd, latency, trace_id)
        return result

    def _check_budget(self, budget, trace_id: str) -> None:
        if budget.kill_switch:
            raise BudgetExceeded("kill switch is on (budget.kill_switch in config/llm.toml)")
        spent = self.store.llm_spend(trace_id=trace_id)
        if spent >= budget.per_message_usd:
            raise BudgetExceeded(f"this message has spent ${spent:.4f}, at its cap of ${budget.per_message_usd:.2f}")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        day = self.store.llm_spend(since=today)
        if day >= budget.per_day_usd:
            raise BudgetExceeded(f"today's spend is ${day:.4f}, at the daily cap of ${budget.per_day_usd:.2f}")


class BoundLlm:
    """The manager bound to one message's trace. This is what pipeline stages hold as ctx.llm."""

    def __init__(self, manager: LLMManager, trace_id: str):
        self.manager, self.trace_id = manager, trace_id

    def complete(self, component: str, prompt: Prompt, user: RedactedText, *, escalate: bool = False) -> LlmResult:
        return self.manager.complete(component, prompt, user, trace_id=self.trace_id, escalate=escalate)

    def has(self, component: str) -> bool:
        return self.manager.has(component)

    def has_escalation(self, component: str) -> bool:
        return self.manager.has_escalation(component)
