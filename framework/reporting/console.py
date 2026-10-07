"""Live console narration. Every tier attempt, LLM call, verification and Excel
write is printed as it happens — nothing runs silently."""

from __future__ import annotations

import sys
from datetime import datetime, timezone


class ConsoleNarrator:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S")

    def line(self, text: str) -> None:
        if self._enabled:
            print(f"[{self._ts()}] {text}", file=sys.stdout, flush=True)

    def step_start(self, test_case_id: str, step_no: int, action: str, description: str) -> None:
        self.line(f"  [{test_case_id}][step {step_no}] {action} -> '{description}'")

    def tier_attempt(self, test_case_id: str, step_no: int, tier: str, outcome: str) -> None:
        self.line(f"    [{test_case_id}][step {step_no}] Tier {tier}: {outcome}")

    def llm_call(self, test_case_id: str, step_no: int, model: str, tokens_in: int, tokens_out: int, confidence: float) -> None:
        self.line(
            f"    [{test_case_id}][step {step_no}] LLM call -> model={model} "
            f"in={tokens_in} out={tokens_out} confidence={confidence:.2f}"
        )

    def llm_io(self, test_case_id: str, step_no: int, user_prompt: str, raw_response: str) -> None:
        """Prints the raw response from the LLM."""
        if not self._enabled:
            return
        separator = "-" * 70
        print(f"\n    {separator}", file=sys.stdout)
        print(f"    [{test_case_id}][step {step_no}] LLM RESPONSE (raw):", file=sys.stdout)
        for line in raw_response.splitlines():
            print(f"    | {line}", file=sys.stdout)
        print(f"    {separator}\n", file=sys.stdout, flush=True)

    def verify(self, test_case_id: str, step_no: int, ok: bool, reason: str) -> None:
        status = "PASS" if ok else "FAIL"
        self.line(f"    [{test_case_id}][step {step_no}] verify: {status} ({reason})")

    def store(self, test_case_id: str, step_no: int, version: int, status: str) -> None:
        self.line(f"    [{test_case_id}][step {step_no}] stored v{version} ({status})")

    def blocked(self, test_case_id: str, step_no: int, reason: str) -> None:
        self.line(f"    [{test_case_id}][step {step_no}] BLOCKED: {reason}")

    def step_result(self, test_case_id: str, step_no: int, status: str) -> None:
        self.line(f"  [{test_case_id}][step {step_no}] => {status}")

    def excel_write(self, path: str) -> None:
        self.line(f"Writing results to workbook: {path}")

    def run_summary(self, run_id: str, totals: dict) -> None:
        self.line(f"Run {run_id} summary: {totals}")
