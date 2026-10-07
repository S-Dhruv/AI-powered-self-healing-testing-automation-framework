"""Per-call LLM usage logging (spec §5 / R7).

Every call (success and error) is appended as one JSON line. Token counts come
only from the API response's usage fields — never estimated locally.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


@dataclass
class UsageLogEntry:
    timestamp: str
    run_id: str
    test_case_id: str
    step_no: int
    user_id: str
    agent_name: str
    reason: Literal["first_learning", "healing"]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    status: Literal["success", "error"]
    estimated_cost: float


class UsageLogger:
    """Appends JSONL usage records. Format documented in README for the dashboard."""

    def __init__(self, log_path: str | Path) -> None:
        self._path = Path(log_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, entry: UsageLogEntry) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
