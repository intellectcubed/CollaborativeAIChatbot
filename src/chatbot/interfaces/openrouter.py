"""OpenRouter client: one chat completions call, returning what came back.

OpenRouter gives access to many providers' models through one API. Every request asks for a JSON schema
response. Effort is not sent: for OpenRouter models, anything beyond model and max_tokens is passed through
`extra` as name-value pairs, and the user is responsible for what each model accepts.
"""

import logging

import httpx

from chatbot.interfaces.llm import LlmError, LlmRefusal, LlmResult, LlmTruncated, ProviderError, _require_redacted
from chatbot.models import RedactedText

URL = "https://openrouter.ai/api/v1/chat/completions"
log = logging.getLogger("llm")


class OpenRouterClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, timeout: float = 60.0):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=timeout)

    def complete(self, *, system: str, user: RedactedText, schema: dict, model: str, max_tokens: int,
                 effort: str | None = None, extra: dict | None = None) -> LlmResult:
        _require_redacted(user)
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": str(user)}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}},
            **(extra or {}),  # reserved names were rejected when the config was read
        }
        if effort and "reasoning" not in (extra or {}):
            body["reasoning"] = {"effort": effort}  # the model's own reasoning control, when the user has not set one
        try:
            r = self.http.post(URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.TransportError as exc:
            raise ProviderError(f"openrouter unreachable: {type(exc).__name__}") from exc
        if r.status_code == 429 or r.status_code >= 500:
            raise ProviderError(f"openrouter answered {r.status_code}")
        if r.status_code >= 400:
            raise LlmError(f"openrouter answered {r.status_code}: {_error_message(r)}")
        data = r.json()
        choice = data["choices"][0]
        finish = choice.get("finish_reason")
        if finish == "content_filter":
            raise LlmRefusal("content filtered")
        if finish == "length":
            raise LlmTruncated("response hit max_tokens")
        usage = data.get("usage") or {}
        cost = usage.get("cost")
        if cost is None:
            log.warning("openrouter did not report a cost for %s: the budget cannot count this call", model)
        return LlmResult(
            text=choice["message"].get("content") or "",
            model=data.get("model", model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            stop_reason=finish,
            request_id=data.get("id"),
            reported_cost_usd=cost,
        )


def _error_message(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error", {}).get("message", ""))[:200]
    except (ValueError, AttributeError):
        return ""
