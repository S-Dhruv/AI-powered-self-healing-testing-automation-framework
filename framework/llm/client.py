"""OpenAI client wrapper for Tier 3 element finding.

Temperature 0, structured JSON output only. Token counts are read exclusively
from the API response's `usage` field — never estimated. Cost is computed from
a documented price table; if a model's price is not in that table, cost is
reported as None rather than fabricated, and the caller must say so in logs.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from openai import APIError, OpenAI

from framework.llm.prompts import SYSTEM_PROMPT, build_user_prompt

# Source: https://openai.com/api/pricing/ — USD per 1M tokens. Documented assumption:
# this table must be kept in sync manually; it is not fetched live. If a model is
# missing here, estimated_cost is None (never guessed) and this must be logged.
PRICE_PER_MILLION_TOKENS_USD: dict[str, tuple[float, float]] = {
    # model: (input_price, output_price)
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
}

# Models that reject an explicit temperature and only support their default
# (1) -- confirmed live for gpt-6-luna: "Unsupported value: 'temperature'
# does not support 0 with this model." Spec §2 wants temperature=0 for
# determinism, so that is still sent for every model except these; here the
# parameter is omitted entirely rather than retried with a different value,
# since this project never silently guesses around a model's own constraint.
_MODELS_WITHOUT_CUSTOM_TEMPERATURE = {"gpt-6-luna"}


@dataclass
class LlmProposal:
    confidence: float
    reason: str
    raw_text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    status: str  # "success" | "error"
    index: int | None = None  # Element index from the numbered list
    # Kept for compatibility with older test doubles/callers. New providers
    # must return an index; selectors are derived locally from the tagged DOM.
    selector: str | None = None
    strategy: str | None = None
    system_prompt: str = ""
    user_prompt: str = ""


class LlmClient:
    def __init__(self, api_key: str, model: str, request_timeout: int) -> None:
        self._client = OpenAI(api_key=api_key, timeout=request_timeout)
        self._model = model
        self._unavailable_reason: str | None = None

    @property
    def model(self) -> str:
        return self._model

    def find_element(
        self,
        *,
        target_description: str,
        action: str,
        aria_snapshot: str = "",
        element_list: str,
        retry_feedback: str | None = None,
    ) -> LlmProposal:
        user_prompt = build_user_prompt(
            target_description=target_description,
            action=action,
            aria_snapshot=aria_snapshot,
            interactive_elements=element_list,
            retry_feedback=retry_feedback,
        )
        started = time.monotonic()
        if self._unavailable_reason is not None:
            return LlmProposal(
                index=None,
                confidence=0.0,
                reason=self._unavailable_reason,
                raw_text="",
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                status="error",
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
            )
        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            # Element identification only needs a small JSON object. Bounding
            # completion size prevents a malformed response from consuming a
            # disproportionate share of the account quota.
            "max_tokens": 256,
        }
        if self._model not in _MODELS_WITHOUT_CUSTOM_TEMPERATURE:
            kwargs["temperature"] = 0
        try:
            response = self._client.chat.completions.create(**kwargs)
        except APIError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            reason = f"LLM API error: {exc}"
            if _is_fatal_quota_error(exc):
                self._unavailable_reason = reason
            return LlmProposal(
                index=None, confidence=0.0,
                reason=reason, raw_text="",
                input_tokens=0, output_tokens=0, latency_ms=latency_ms, status="error",
                system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else 0
        output_tokens = usage.completion_tokens if usage else 0
        raw_text = response.choices[0].message.content or ""

        parsed = _parse_structured_output(raw_text)
        if parsed is None:
            return LlmProposal(
                index=None, confidence=0.0,
                reason="LLM output was not valid structured JSON; discarded",
                raw_text=raw_text, input_tokens=input_tokens, output_tokens=output_tokens,
                latency_ms=latency_ms, status="success",
                system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt,
            )
        return LlmProposal(
            index=parsed.get("index"),
            confidence=float(parsed.get("confidence", 0.0)),
            reason=parsed.get("reason", ""),
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            raw_text=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            status="success",
        )


def _is_fatal_quota_error(exc: APIError) -> bool:
    """Identify errors for which retrying cannot succeed during this run."""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status in {401, 403, 429} or bool(
        re.search(r"insufficient_quota|no credits|expired|invalidated", text)
    )


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = PRICE_PER_MILLION_TOKENS_USD.get(model)
    if prices is None:
        return None
    input_price, output_price = prices
    return (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price


def _parse_structured_output(raw_text: str) -> dict | None:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "index" not in data or "confidence" not in data:
        return None
    try:
        index = int(data.get("index", -1))
        if index < 0:
            return None
    except (ValueError, TypeError):
        return None
    # Discard anything beyond the strict schema fields.
    return {
        "index": index,
        "confidence": data.get("confidence"),
        "reason": data.get("reason", ""),
    }
