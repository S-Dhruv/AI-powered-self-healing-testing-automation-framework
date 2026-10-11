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
            self._print_safe(f"[{self._ts()}] {text}", flush=True)

    def _print_safe(self, text: str, *, flush: bool = False) -> None:
        """Keep console reporting reliable on legacy Windows code pages."""
        output = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = text.encode(output, errors="replace").decode(output, errors="replace")
        print(safe, file=sys.stdout, flush=flush)

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
        """Prints both the exact prompt sent to the LLM and its exact raw
        response -- the dashboard parses both blocks out of this same
        console stream (never a second, separate transport) so a developer
        can see precisely what the model was given and precisely what it
        said back, not just the short reason string."""
        if not self._enabled:
            return
        separator = "-" * 70
        self._print_safe(f"\n    {separator}")
        self._print_safe(f"    [{test_case_id}][step {step_no}] LLM REQUEST (prompt):")
        for line in user_prompt.splitlines():
            self._print_safe(f"    | {line}")
        self._print_safe(f"    {separator}")
        self._print_safe(f"    [{test_case_id}][step {step_no}] LLM RESPONSE (raw):")
        for line in raw_response.splitlines():
            self._print_safe(f"    | {line}")
        self._print_safe(f"    {separator}\n", flush=True)

    def verify(self, test_case_id: str, step_no: int, ok: bool, reason: str) -> None:
        status = "PASS" if ok else "FAIL"
        self.line(f"    [{test_case_id}][step {step_no}] verify: {status} ({reason})")

    def store(self, test_case_id: str, step_no: int, version: int, status: str) -> None:
        self.line(f"    [{test_case_id}][step {step_no}] stored v{version} ({status})")

    def blocked(self, test_case_id: str, step_no: int, reason: str) -> None:
        self.line(f"    [{test_case_id}][step {step_no}] BLOCKED: {reason}")

    def action(self, test_case_id: str, step_no: int, verb: str, selector: str) -> None:
        """Logged at the moment an action actually touches the page -- distinct
        from step_result (the step's final outcome) so a live run shows exactly
        which element was clicked/typed into/selected/checked, in order, as it
        happens, not just the summary after the fact."""
        self.line(f"    [{test_case_id}][step {step_no}] ACTION {verb} -> {selector}")

    def step_result(self, test_case_id: str, step_no: int, status: str) -> None:
        self.line(f"  [{test_case_id}][step {step_no}] => {status}")

    def excel_write(self, path: str) -> None:
        self.line(f"Writing results to workbook: {path}")

    def run_summary(self, run_id: str, totals: dict) -> None:
        self.line(f"Run {run_id} summary: {totals}")
