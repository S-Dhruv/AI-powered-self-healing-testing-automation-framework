"""Test doubles. Per claude.md: mock the LLM in framework tests, never hit the real API."""

from __future__ import annotations

from dataclasses import dataclass

from framework.llm.client import LlmProposal


@dataclass
class ScriptedResponse:
    selector: str | None
    strategy: str | None
    confidence: float
    reason: str = "scripted"
    input_tokens: int = 120
    output_tokens: int = 40
    status: str = "success"


class FakeLlmClient:
    """Returns pre-scripted responses in order, one per call. Never calls the network."""

    def __init__(self, responses: list[ScriptedResponse], model: str = "gpt-4o-mini") -> None:
        self._responses = list(responses)
        self._model = model
        self.calls = 0

    @property
    def model(self) -> str:
        return self._model

    def find_element(self, **kwargs) -> LlmProposal:
        self.calls += 1
        if not self._responses:
            raise AssertionError("FakeLlmClient ran out of scripted responses")
        r = self._responses.pop(0)
        return LlmProposal(
            selector=r.selector, strategy=r.strategy, confidence=r.confidence, reason=r.reason,
            raw_text="{}", input_tokens=r.input_tokens, output_tokens=r.output_tokens,
            latency_ms=5, status=r.status,
        )
